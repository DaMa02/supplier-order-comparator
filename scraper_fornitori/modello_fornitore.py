#!/usr/bin/env python3
"""Template to copy for a new supplier's scraper.

Copy this file to `scraper_<fornitore>.py`, fill in the three parts marked
`DA SCRIVERE` and run that. Everything else — session, resuming, CSV,
metadata — lives in `scraper_fornitore.py` and shouldn't be copied.

As it stands, it doesn't work on purpose: the three methods raise an
error naming the missing piece. That keeps a file that looks ready from
sitting around silently downloading error pages instead.

Before writing a line of code:

1. log into the site in a browser and reach the product listing;
2. open dev tools, Network tab, and change pages in the listing: usually
   the page doesn't reload everything but calls an endpoint that returns
   just the rows. That endpoint is the one to call, not the full page;
3. look at what's sent to the login (hidden fields included) and what
   changes in the row endpoint's address when the page changes;
4. fill in the three parts below and try with `--max-pagine 2 --reset`.
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


# DA SCRIVERE — the site's endpoints.
INDIRIZZO_BASE = "https://esempio.invalid"
INDIRIZZO_ACCESSO = f"{INDIRIZZO_BASE}/login"
INDIRIZZO_ELENCO = f"{INDIRIZZO_BASE}/catalogo"
INDIRIZZO_RIGHE = f"{INDIRIZZO_BASE}/catalogo/righe"


class SitoDaCompletare(SitoFornitore):
    # Short lowercase supplier id: goes into the CSV filename
    # (`catalogo_<nome>.csv`) and the credential environment variables
    # (`<NOME>_UTENTE`, `<NOME>_PASSWORD`).
    nome = "fornitore"

    # The CSV columns. Also the keys `pagina()` must return. Worth keeping
    # `pagina_catalogo`: it's how to trace back to the right row when a
    # check doesn't add up.
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

    # -- 1. login -------------------------------------------------------

    def accedi(self, cliente: ClienteHttp, credenziali: Credenziali) -> None:
        """DA SCRIVERE: submit the login form and check it worked.

        The pattern below fits ASP.NET WebForms portals, which are the
        majority: read the page, send back every hidden field as received,
        and add username, password and the submit button's name.
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

        # The check must be on something that appears ONLY once logged in:
        # a failed login that proceeds fills the CSV with error pages, and
        # nobody notices until they look at the file.
        if "esci" not in risposta.lower():
            raise ScraperError("accesso non riuscito: controlla le credenziali o l'account")

        raise NotImplementedError(
            "controlla gli indirizzi, i nomi dei campi e la prova dell'avvenuto accesso, "
            "poi togli questa riga"
        )

    # -- 2. how many pages there are -------------------------------------------

    def panoramica(self, cliente: ClienteHttp) -> Panoramica:
        """DA SCRIVERE: read from the site how many pages, and the item count if given.

        Don't hardcode a number here: the catalog grows, and a scraper
        pinned to a hand-written number silently stops picking up new pages.
        """

        raise NotImplementedError(
            "trova nell'HTML dell'elenco il campo che dice il numero di pagine "
            "(spesso un input nascosto o il testo «Pagina 1 di N») e restituisci "
            "Panoramica(pagine=..., articoli_dichiarati=...)"
        )

    # -- 3. a page's rows ------------------------------------------

    def pagina(self, cliente: ClienteHttp, numero: int) -> list[dict[str, Any]]:
        """DA SCRIVERE: download a page and map it into `campi_csv` keys.

        `righe_di_tabella` returns the cells as text, row by row: what's
        left is skipping the header row and naming the columns. The number
        of columns must be checked, not assumed: a short row is a header or
        separator row, and if it ends up in the CSV it shifts every value
        one column over.
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
