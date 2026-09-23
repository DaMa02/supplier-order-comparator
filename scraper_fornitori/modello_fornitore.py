#!/usr/bin/env python3
"""Modello da copiare per lo scraper di un fornitore nuovo.

Si copia questo file in `scraper_<fornitore>.py`, si riempiono le tre parti
segnate con `DA SCRIVERE` e si avvia quello. Tutto il resto — sessione,
ripresa, CSV, metadati — sta in `scraper_fornitore.py` e non va ricopiato.

Cosi' com'e' **non funziona di proposito**: i tre metodi sollevano un errore
che dice quale pezzo manca. Serve a non avere in giro un file che sembra
pronto e invece scarica pagine d'errore.

Il giro, prima di scrivere una riga di codice:

1. entra nel sito col browser e arriva all'elenco dei prodotti;
2. apri gli strumenti per sviluppatori, scheda Rete, e cambia pagina
   nell'elenco: quasi sempre la pagina non ricarica tutto, ma chiama un
   indirizzo che restituisce le sole righe. **Quello** e' l'indirizzo da
   chiamare, non la pagina intera;
3. guarda che cosa viene mandato al login (campi nascosti compresi) e che
   cosa cambia nell'indirizzo delle righe quando cambi pagina;
4. riempi le tre parti qui sotto e prova con `--max-pagine 2 --reset`.
"""

from __future__ import annotations

from typing import Any

from scraper_fornitore import (
    ClienteHttp,
    Credenziali,
    Panoramica,
    ScraperError,
    SitoFornitore,
    analizza_form,
    avvia,
    righe_di_tabella,
)


# DA SCRIVERE — gli indirizzi del sito.
INDIRIZZO_BASE = "https://esempio.invalid"
INDIRIZZO_ACCESSO = f"{INDIRIZZO_BASE}/login"
INDIRIZZO_ELENCO = f"{INDIRIZZO_BASE}/catalogo"
INDIRIZZO_RIGHE = f"{INDIRIZZO_BASE}/catalogo/righe"


class SitoDaCompletare(SitoFornitore):
    # Identificativo breve del fornitore, in minuscolo: finisce nel nome del
    # CSV (`catalogo_<nome>.csv`) e nelle variabili d'ambiente delle
    # credenziali (`<NOME>_UTENTE`, `<NOME>_PASSWORD`).
    nome = "fornitore"

    # Le colonne del CSV. Sono anche le chiavi che `pagina()` deve
    # restituire. Conviene tenere `pagina_catalogo`: e' il modo per tornare
    # sulla riga giusta quando un controllo non torna.
    campi_csv = (
        "pagina_catalogo",
        "ean",
        "prodotto",
        "confezione",
        "disponibilita",
        "prezzo",
        "unita",
    )

    righe_per_pagina = 50

    # -- 1. l'accesso -------------------------------------------------------

    def accedi(self, cliente: ClienteHttp, credenziali: Credenziali) -> None:
        """DA SCRIVERE: manda il form di login e controlla che sia andato.

        Lo schema qui sotto vale per i gestionali web su ASP.NET WebForms,
        che sono la maggioranza: si legge la pagina, si rimandano indietro
        tutti i campi nascosti come sono arrivati, e si aggiungono utente,
        password e il nome del pulsante.
        """

        html = cliente.leggi(INDIRIZZO_ACCESSO)
        form = analizza_form(html)
        campo_utente = form.trova_campo("username")
        campo_password = form.trova_campo("password")
        pulsante = form.trova_campo("loginbutton")
        if not campo_utente or not campo_password or not pulsante:
            raise ScraperError("il form di accesso non ha i campi attesi: il sito e' cambiato")

        valori = form.valori_nascosti()
        valori[campo_utente["name"]] = credenziali.utente
        valori[campo_password["name"]] = credenziali.password
        valori[pulsante["name"]] = pulsante.get("value", "Accedi")
        risposta = cliente.invia(INDIRIZZO_ACCESSO, valori)

        # Il controllo va fatto su qualcosa che compare SOLO da dentro: un
        # login fallito che prosegue riempie il CSV di pagine d'errore, e
        # nessuno se ne accorge fino a quando non si guarda il file.
        if "esci" not in risposta.lower():
            raise ScraperError("accesso non riuscito: controlla le credenziali o l'account")

        raise NotImplementedError(
            "controlla gli indirizzi, i nomi dei campi e la prova dell'avvenuto accesso, "
            "poi togli questa riga"
        )

    # -- 2. quante pagine ci sono -------------------------------------------

    def panoramica(self, cliente: ClienteHttp) -> Panoramica:
        """DA SCRIVERE: leggi dal sito quante pagine, e se lo dice quanti articoli.

        Non scrivere qui un numero fisso: il catalogo cresce, e uno scraper
        che si ferma a un numero scritto a mano smette di prendere la coda
        senza dirlo a nessuno.
        """

        raise NotImplementedError(
            "trova nell'HTML dell'elenco il campo che dice il numero di pagine "
            "(spesso un input nascosto o il testo «Pagina 1 di N») e restituisci "
            "Panoramica(pagine=..., articoli_dichiarati=...)"
        )

    # -- 3. le righe di una pagina ------------------------------------------

    def pagina(self, cliente: ClienteHttp, numero: int) -> list[dict[str, Any]]:
        """DA SCRIVERE: scarica una pagina e traducila nelle chiavi di `campi_csv`.

        `righe_di_tabella` restituisce le celle come testo, riga per riga:
        resta da saltare l'intestazione e dare un nome alle colonne. Il
        numero di colonne va controllato, non dato per buono: una riga corta
        e' una riga d'intestazione o un separatore, e se finisce nel CSV
        sposta tutti i valori di una casella.
        """

        html = cliente.leggi(f"{INDIRIZZO_RIGHE}?pagina={numero}")
        righe: list[dict[str, Any]] = []
        for celle in righe_di_tabella(html, id_tabella="tabella-prodotti"):
            if len(celle) < len(self.campi_csv) - 1:
                continue
            righe.append(
                {
                    "pagina_catalogo": numero,
                    "ean": celle[0],
                    "prodotto": celle[1],
                    "confezione": celle[2],
                    "disponibilita": celle[3],
                    "prezzo": celle[4],
                    "unita": celle[5],
                }
            )
        raise NotImplementedError(
            "sistema l'indirizzo delle righe, l'id della tabella e l'ordine delle celle, "
            "poi togli questa riga"
        )
        return righe


if __name__ == "__main__":
    raise SystemExit(avvia(SitoDaCompletare()))
