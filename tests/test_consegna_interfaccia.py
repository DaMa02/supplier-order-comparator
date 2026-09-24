"""Browser side of delivery: price-list download and past runs panel.

The local service can be entirely correct — dated folders, in-memory zips,
paths that stay inside the root — and the page can still fail to deliver
anything: a download button that never appears, one that appears when the zip
doesn't exist and 404s, a run hidden because its audit can't be parsed, a file
name with an em dash written into the HTML without going through escapeHtml.
None of these four failure modes is visible from a backend test: they all live
in app/static/app.js.

As in SupplierMoveInterfaceTests and ImpostazioniInterfacciaTests, these tests
assert against the JS source text. Each assertion is tied to the text that
actually drives the behavior, so disabling the defense turns it red.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]

# Classes the delivery panel writes into the HTML; each needs a matching
# rule in styles.css or the panel renders unstyled.
CLASSI_DELLA_CONSEGNA = (
    "results__consegna",
    "results__zip-nome",
    "compilazioni-panel",
    "compilazioni",
    "compilazione",
    "is-incompleta",
    "compilazione__head",
    "compilazione__meta",
    "compilazione__nota",
    "compilazione__fornitori",
    "compilazione__consegna",
    "compilazione__zip-nome",
    "compilazione__file",
    "compilazione__tipo",
)

# Names that, inside the render functions, carry data from the local service:
# file names with spaces and an em dash, labels, totals.
DATI_DAL_SERVIZIO = (
    "voce",
    "documento",
    "documenti",
    "output",
    "fornitore",
    "etichetta",
    "cartella",
    "zipNome",
    "totale",
    "righe",
    "nome",
    "tipo",
    "state.compileResult",
)

# The only paths through which service data may reach the HTML.
DISINFETTANTI = (
    "escapeHtml(",
    "formatEuro(",
    "formatInteger(",
    "renderBadge(",
    "renderAlert(",
    "renderCompilazione)",
    "renderCompilazioneFile)",
)


def interpolazioni_foglia(testo: str) -> list[str]:
    """Return the `${...}` interpolations that contain no nested ones.

    That's where a value actually lands in the HTML: an outer `${...}` that
    contains others is just a branch (`x ? ... : ...`), and checking it
    instead of its children would let an unsanitized branch pass as clean.
    """

    trovate: list[str] = []
    indice = 0
    while True:
        inizio = testo.find("${", indice)
        if inizio < 0:
            return trovate
        profondita = 0
        fine = inizio + 1
        while fine < len(testo):
            if testo[fine] == "{":
                profondita += 1
            elif testo[fine] == "}":
                profondita -= 1
                if profondita == 0:
                    break
            fine += 1
        else:  # pragma: no cover - would mean app.js doesn't parse
            raise AssertionError("interpolazione non chiusa in app.js")
        contenuto = testo[inizio + 2 : fine]
        interne = interpolazioni_foglia(contenuto)
        trovate.extend(interne or [contenuto])
        indice = fine + 1


class ConsegnaInterfacciaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app_js = (SKILL_ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.css = (SKILL_ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.app_js, f"manca la funzione {name}")
        return self.app_js.split(marker)[1].split("\n}\n")[0]

    # --- Removed feature: the Noce site scraper --------------------------

    def test_della_macchina_del_sito_noce_non_resta_niente_nella_pagina(self) -> None:
        """The page must not reference the Noce site importer (credentials,
        site import, cart). Any action, route or class from it showing up
        here would mean the feature is creeping back in, and no backend test
        would catch it.
        """

        for residuo in (
            "noce-import-card",
            "noce-credentials",
            "noce-cart-card",
            "data-noce-credential",
            "import-noce",
            "prepare-noce-cart",
            "/api/noce/",
            "NoceCredentials",
        ):
            self.assertNotIn(residuo, self.app_js, residuo)
            self.assertNotIn(residuo, self.css, residuo)

    # --- Download button ----------------------------------------------------

    def test_il_pulsante_compare_solo_quando_lo_zip_esiste(self) -> None:
        """With no price lists there's nothing to deliver, and a button that
        downloads a 404 is worse than no button."""

        corpo = self.body("renderCompileResult")
        self.assertIn("const zipUrl = safeDownloadUrl(state.compileResult.zipUrl);", corpo)
        riga = self.riga_con(corpo, "Scarica i listini pronti per l’invio")
        # The label must sit inside the branch that already checked the zip.
        self.assertIn("${zipUrl ?", riga)

    def test_il_pulsante_e_un_collegamento_scaricabile_non_un_bottone(self) -> None:
        """An `<a download>` works without extra JavaScript and shows the
        target URL in the status bar before it's clicked."""

        riga = self.riga_con(self.body("renderCompileResult"), "Scarica i listini pronti per l’invio")
        self.assertIn("<a ", riga)
        self.assertIn(' download>', riga)
        self.assertIn('href="${escapeHtml(zipUrl)}"', riga)
        self.assertNotIn("<button", riga)

    def test_ogni_indirizzo_passa_da_safeDownloadUrl(self) -> None:
        for funzione in ("renderCompileResult", "renderCompilazione", "renderCompilazioneFile"):
            corpo = self.body(funzione)
            indirizzi = re.findall(r'href="\$\{([^}]+)\}"', corpo)
            self.assertTrue(indirizzi, f"{funzione} non disegna nessun collegamento")
            for espressione in indirizzi:
                self.assertTrue(
                    espressione.startswith("escapeHtml("),
                    f"{funzione}: l'indirizzo {espressione} finisce nell'HTML senza escapeHtml",
                )
                variabile = espressione[len("escapeHtml(") : -1]
                self.assertRegex(
                    corpo,
                    r"const\s+" + re.escape(variabile) + r"\s*=\s*safeDownloadUrl\(",
                    f"{funzione}: {variabile} non viene da safeDownloadUrl",
                )

    def test_gli_indirizzi_non_si_costruiscono_nel_browser(self) -> None:
        """File names carry spaces and an em dash; the encoding is done by the
        local service, the same one that wrote the files. Redoing it here
        just means getting it wrong differently."""

        for funzione in ("renderCompilazione", "renderCompilazioneFile", "renderCompilazioniPrecedenti"):
            corpo = self.body(funzione)
            self.assertNotIn("/ordini/", corpo, f"{funzione} costruisce un indirizzo invece di usare quello del servizio")
            self.assertNotIn("encodeURIComponent", corpo)

    # --- Past runs panel ------------------------------------------------

    def test_il_riquadro_sta_nel_passo_tre(self) -> None:
        self.assertIn("renderCompilazioniPrecedenti()", self.body("renderCompileStep"))

    def test_lo_stato_dello_storico_conserva_il_contratto_e_la_cancellazione(self) -> None:
        stato = self.app_js.split("compilazioni: {", 1)[1].split("},", 1)[0]
        for campo in (
            "caricate: false",
            "inCorso: false",
            "elenco: []",
            'errore: ""',
            "aperta: false",
            'confermaElimina: ""',
            'eliminando: ""',
        ):
            self.assertIn(campo, stato)

    def test_lo_storico_si_chiede_a_api_ordini(self) -> None:
        indirizzi = self.app_js.split("const API = {")[1].split("};")[0]
        self.assertIn('"/api/ordini"', indirizzi)
        self.assertIn("requestJson(API.ordini)", self.body("loadCompilazioni"))

    def test_lo_storico_si_carica_entrando_nel_riepilogo(self) -> None:
        self.assertIn("if (nextStep === 3) loadCompilazioni();", self.body("goToStep"))
        # Also for someone reopening the page already on step 3, who never goes through goToStep.
        self.assertIn("if (state.currentStep === 3) loadCompilazioni();", self.body("loadReview"))

    def test_lo_storico_si_ricarica_dopo_ogni_compilazione_riuscita(self) -> None:
        """The run that just finished is the newest entry in the list; without
        this second request the panel stays one run behind."""

        corpo = self.body("compileOrders")
        self.assertIn("loadCompilazioni();", corpo)
        # In the success branch only, not in catch/finally: after an error
        # there's no new folder to show.
        prima_del_catch = corpo.split("} catch (error) {")[0]
        self.assertIn("loadCompilazioni();", prima_del_catch)

    def test_in_modalita_dimostrativa_non_si_chiama_niente(self) -> None:
        corpo = self.body("loadCompilazioni")
        prima_della_richiesta = corpo.split("requestJson(")[0]
        self.assertIn('mode === "demo"', prima_della_richiesta)
        self.assertIn("return;", prima_della_richiesta)
        # And the panel doesn't render at all in demo mode, since demo data
        # has no real runs.
        self.assertIn('if (mode === "demo") return "";', self.body("renderCompilazioniPrecedenti"))

    def test_un_elenco_che_non_si_carica_e_un_avviso_non_un_passo_tre_rotto(self) -> None:
        corpo = self.body("loadCompilazioni")
        # `state.runtimeError` renders the page-wide red alert: a history that
        # fails to load is a missing list, not an app crash.
        self.assertNotIn("state.runtimeError", corpo)
        self.assertIn("state.compilazioni.errore = `Storico delle compilazioni non letto:", corpo)
        # The stale list doesn't survive the error: it would show runs that
        # were never re-read from disk as present.
        self.assertIn("state.compilazioni.elenco = [];", corpo.split("} catch (error) {")[1])

        riquadro = self.body("renderCompilazioniPrecedenti")
        self.assertIn("storico.errore", riquadro)
        self.assertIn("renderAlert(", riquadro)
        # The panel doesn't bail out on error: it's exactly where the error
        # should be reported.
        self.assertNotIn("errore) return", riquadro)

        # And the rest of step 3 doesn't depend on the history in any way.
        passo = self.body("renderCompileStep")
        self.assertNotIn("state.compilazioni", passo)

    def test_prima_della_risposta_non_si_dice_che_non_ce_n_e_nessuna(self) -> None:
        """An empty list because nothing has loaded yet isn't an empty list:
        telling a user who just ran ten comparisons that none are registered
        is a lie, however brief."""

        corpo = self.body("renderCompilazioniPrecedenti")
        self.assertIn("const inAttesa = storico.inCorso || (!storico.caricate && !storico.errore);", corpo)
        riga_vuoto = self.riga_con(corpo, "Nessuna compilazione registrata")
        self.assertIn("!inAttesa", riga_vuoto)
        # Only a successful request sets that flag.
        caricamento = self.body("loadCompilazioni")
        self.assertIn("state.compilazioni.caricate = true;", caricamento.split("} catch (error) {")[0])
        self.assertNotIn("state.compilazioni.caricate = true;", caricamento.split("} catch (error) {")[1])

    def test_la_voce_incompleta_si_mostra_lo_stesso_dicendolo(self) -> None:
        """The folder exists and its files are downloadable; hiding it would
        make available price lists disappear."""

        riquadro = self.body("renderCompilazioniPrecedenti")
        self.assertIn("voci.map(renderCompilazione)", riquadro)
        self.assertNotIn(".filter(", riquadro)

        corpo = self.body("renderCompilazione")
        self.assertIn("const completa = voce?.completa !== false;", corpo)
        self.assertIn('completa ? "" :', corpo)
        self.assertIn("non si leggono", corpo)
        self.assertIn("Dettagli non leggibili", corpo)

    def test_i_documenti_della_voce_incompleta_restano_scaricabili(self) -> None:
        corpo = self.body("renderCompilazione")
        riga_zip = self.riga_con(corpo, "Scarica i listini di questa compilazione")
        self.assertNotIn("completa", riga_zip, "lo zip non deve dipendere dalla leggibilita' dell'audit")
        riga_file = self.riga_con(corpo, "renderCompilazioneFile")
        self.assertNotIn("completa", riga_file, "i singoli file non devono dipendere dalla leggibilita' dell'audit")
        # The zip button still depends on the URL being present, same as on step 3.
        self.assertIn("${zipUrl ?", riga_zip)
        self.assertIn("const zipUrl = safeDownloadUrl(voce?.zipUrl);", corpo)

    def test_l_ordine_dell_elenco_e_quello_del_servizio(self) -> None:
        """Dates are read from disk by the local service; sorting again here
        would create two orderings that can drift apart."""

        for funzione in ("renderCompilazioniPrecedenti", "renderCompilazione", "loadCompilazioni"):
            self.assertNotIn(".sort(", self.body(funzione))

    def test_nessun_totale_viene_ricalcolato_nel_browser(self) -> None:
        for funzione in ("renderCompilazione", "renderCompilazioniPrecedenti", "compilazioneMeta"):
            corpo = self.body(funzione)
            for vietato in (".reduce(", "+=", "* "):
                self.assertNotIn(vietato, corpo, f"{funzione} sembra ricalcolare un numero del servizio")
        corpo = self.body("renderCompilazione")
        # formatEuro(null) would print "0,00 €": a missing total isn't a zero.
        self.assertIn("totale == null", corpo)
        self.assertIn("fornitore?.totaleNetto == null", corpo)

    def test_ogni_testo_del_servizio_passa_da_escapeHtml(self) -> None:
        """File names contain spaces and an em dash, and will eventually
        contain a quote or apostrophe: what the service sends isn't HTML and
        must never be allowed to become HTML."""

        for funzione in ("renderCompileResult", "renderCompilazione", "renderCompilazioneFile"):
            corpo = self.body(funzione)
            foglie = interpolazioni_foglia(corpo)
            self.assertTrue(foglie, f"{funzione} non interpola niente: la prova non sta guardando il codice giusto")
            for foglia in foglie:
                if not any(dato in foglia for dato in DATI_DAL_SERVIZIO):
                    continue
                self.assertTrue(
                    any(pulito in foglia for pulito in DISINFETTANTI),
                    f"{funzione}: «{foglia.strip()}» finisce nell'HTML senza passare da escapeHtml",
                )

    def test_lo_stato_di_una_compilazione_si_traduce_senza_inventare(self) -> None:
        tabella = self.app_js.split("const STATI_COMPILAZIONE = {")[1].split("\n};")[0]
        for stato in ("FILES_READY", "PLAN_READY", "SCONOSCIUTO"):
            self.assertIn(stato, tabella, f"il browser non sa tradurre {stato}")
        # A status the table doesn't recognize is shown as-is: an invented
        # translation would mask a contract change.
        self.assertIn("STATI_COMPILAZIONE[stato] || stato", self.body("compilazioneMeta"))

    # --- Stylesheet ----------------------------------------------------------

    def test_le_classi_della_consegna_hanno_uno_stile(self) -> None:
        for classe in CLASSI_DELLA_CONSEGNA:
            self.assertIn(classe, self.app_js, f"nessuno scrive la classe {classe}")
            self.assertIn(f".{classe}", self.css, f"la classe {classe} non ha nessuna regola in styles.css")

    # --- Helpers ---------------------------------------------------------

    def riga_con(self, corpo: str, testo: str) -> str:
        righe = [riga for riga in corpo.splitlines() if testo in riga]
        self.assertEqual(len(righe), 1, f"«{testo}» compare {len(righe)} volte, ne serve una sola")
        return righe[0]


if __name__ == "__main__":
    unittest.main()
