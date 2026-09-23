"""Fase 6d, lato browser: la consegna dei listini e le compilazioni precedenti.

Il servizio locale puo' avere ragione su tutto — cartelle datate, zip in memoria,
percorsi che non escono dalla radice — e la pagina puo' lo stesso non consegnare
niente: un pulsante che non compare, un pulsante che compare quando lo zip non
c'e' e scarica un 404, una compilazione nascosta perche' il suo audit non si
legge, un nome di file con l'em dash scritto nell'HTML senza passare da
escapeHtml. Nessuna di queste quattro cose puo' essere vista da un test del
servizio: vivono tutte in app/static/app.js.

Come in SupplierMoveInterfaceTests e in ImpostazioniInterfacciaTests, le prove
sono asserzioni sul testo sorgente. Ogni asserzione e' agganciata al testo che
porta davvero il comportamento: spegnere la difesa la rende rossa.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


SKILL_ROOT = Path(__file__).resolve().parents[1]

# Le classi introdotte dalla 6d: quello che app.js scrive nell'HTML deve avere
# uno stile, altrimenti il riquadro esce impaginato a caso.
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

# I nomi che dentro le funzioni di disegno contengono dati arrivati dal servizio
# locale: nomi di file con spazi e un em dash, etichette, totali.
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

# Le sole strade per cui un dato del servizio puo' arrivare nell'HTML.
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
    """Gli `${...}` che non ne contengono altri dentro.

    E' li' che il valore finisce davvero nell'HTML: un `${...}` esterno che ne
    contiene altri e' solo un ramo (`x ? ... : ...`), e guardare lui invece dei
    suoi figli farebbe passare per disinfettato un ramo che non lo e'.
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
        else:  # pragma: no cover - vorrebbe dire app.js non compilabile
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

    # --- Lo smontaggio della macchina del sito Noce --------------------

    def test_della_macchina_del_sito_noce_non_resta_niente_nella_pagina(self) -> None:
        """Smontaggio del 12 agosto 2026: credenziali, importazione dal sito,
        carrello.  La pagina non deve piu' nominarli: un'azione, una rotta o
        una classe che ricomparisse qui sarebbe la macchina del sito che
        rientra dalla finestra, e nessun test del servizio la vedrebbe.
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

    # --- Il pulsante della consegna ----------------------------------------

    def test_il_pulsante_compare_solo_quando_lo_zip_esiste(self) -> None:
        """Senza listini non c'e' niente da consegnare, e un pulsante che scarica
        un 404 e' peggio di nessun pulsante."""

        corpo = self.body("renderCompileResult")
        self.assertIn("const zipUrl = safeDownloadUrl(state.compileResult.zipUrl);", corpo)
        riga = self.riga_con(corpo, "Scarica i listini pronti per l’invio")
        # L'etichetta deve stare dentro il ramo che ha gia' verificato lo zip.
        self.assertIn("${zipUrl ?", riga)

    def test_il_pulsante_e_un_collegamento_scaricabile_non_un_bottone(self) -> None:
        """Un <a download> funziona senza altro JavaScript e mostra l'indirizzo
        nella barra di stato prima che lo si prema."""

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
        """I nomi hanno spazi e un em dash: la codifica la fa il servizio locale,
        che e' lo stesso che ha scritto i file. Rifarla qui vuol dire sbagliarla
        in modo diverso."""

        for funzione in ("renderCompilazione", "renderCompilazioneFile", "renderCompilazioniPrecedenti"):
            corpo = self.body(funzione)
            self.assertNotIn("/ordini/", corpo, f"{funzione} costruisce un indirizzo invece di usare quello del servizio")
            self.assertNotIn("encodeURIComponent", corpo)

    # --- Il riquadro delle compilazioni precedenti --------------------------

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
        # E anche a chi riapre la pagina gia' sul passo 3, che non passa da goToStep.
        self.assertIn("if (state.currentStep === 3) loadCompilazioni();", self.body("loadReview"))

    def test_lo_storico_si_ricarica_dopo_ogni_compilazione_riuscita(self) -> None:
        """La compilazione appena fatta e' la prima voce dell'elenco: senza la
        seconda richiesta il riquadro resta indietro di una."""

        corpo = self.body("compileOrders")
        self.assertIn("loadCompilazioni();", corpo)
        # Nel ramo riuscito, non nel catch e non nel finally: dopo un errore non
        # c'e' nessuna cartella nuova da mostrare.
        prima_del_catch = corpo.split("} catch (error) {")[0]
        self.assertIn("loadCompilazioni();", prima_del_catch)

    def test_in_modalita_dimostrativa_non_si_chiama_niente(self) -> None:
        corpo = self.body("loadCompilazioni")
        prima_della_richiesta = corpo.split("requestJson(")[0]
        self.assertIn('mode === "demo"', prima_della_richiesta)
        self.assertIn("return;", prima_della_richiesta)
        # E il riquadro non compare affatto con i dati dimostrativi, che di
        # compilazioni vere non ne hanno nessuna.
        self.assertIn('if (mode === "demo") return "";', self.body("renderCompilazioniPrecedenti"))

    def test_un_elenco_che_non_si_carica_e_un_avviso_non_un_passo_tre_rotto(self) -> None:
        corpo = self.body("loadCompilazioni")
        # state.runtimeError disegna l'avviso rosso in cima a tutta la pagina:
        # lo storico non e' un guasto del programma, e' un elenco che manca.
        self.assertNotIn("state.runtimeError", corpo)
        self.assertIn("state.compilazioni.errore = `Storico delle compilazioni non letto:", corpo)
        # L'elenco vecchio non sopravvive all'errore: mostrerebbe come presenti
        # compilazioni che nessuno ha appena riletto sul disco.
        self.assertIn("state.compilazioni.elenco = [];", corpo.split("} catch (error) {")[1])

        riquadro = self.body("renderCompilazioniPrecedenti")
        self.assertIn("storico.errore", riquadro)
        self.assertIn("renderAlert(", riquadro)
        # Il riquadro non si interrompe sull'errore: e' proprio il posto dove
        # l'errore va detto.
        self.assertNotIn("errore) return", riquadro)

        # E il resto del passo 3 non dipende dallo storico in nessun modo.
        passo = self.body("renderCompileStep")
        self.assertNotIn("state.compilazioni", passo)

    def test_prima_della_risposta_non_si_dice_che_non_ce_n_e_nessuna(self) -> None:
        """L'elenco vuoto perche' nessuno l'ha ancora letto non e' un elenco
        vuoto: «nessuna compilazione registrata» detto a chi ne ha appena fatte
        dieci e' una bugia, per quanto breve."""

        corpo = self.body("renderCompilazioniPrecedenti")
        self.assertIn("const inAttesa = storico.inCorso || (!storico.caricate && !storico.errore);", corpo)
        riga_vuoto = self.riga_con(corpo, "Nessuna compilazione registrata")
        self.assertIn("!inAttesa", riga_vuoto)
        # E la richiesta riuscita e' l'unica cosa che alza quella bandiera.
        caricamento = self.body("loadCompilazioni")
        self.assertIn("state.compilazioni.caricate = true;", caricamento.split("} catch (error) {")[0])
        self.assertNotIn("state.compilazioni.caricate = true;", caricamento.split("} catch (error) {")[1])

    def test_la_voce_incompleta_si_mostra_lo_stesso_dicendolo(self) -> None:
        """La cartella c'e' e i suoi file si scaricano: nasconderla farebbe
        sparire dei listini che invece esistono."""

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
        # Lo zip resta pero' legato alla presenza dell'indirizzo, come al passo 3.
        self.assertIn("${zipUrl ?", riga_zip)
        self.assertIn("const zipUrl = safeDownloadUrl(voce?.zipUrl);", corpo)

    def test_l_ordine_dell_elenco_e_quello_del_servizio(self) -> None:
        """Le date le legge il servizio locale sul disco: riordinare qui vuol dire
        avere due ordinamenti che possono divergere."""

        for funzione in ("renderCompilazioniPrecedenti", "renderCompilazione", "loadCompilazioni"):
            self.assertNotIn(".sort(", self.body(funzione))

    def test_nessun_totale_viene_ricalcolato_nel_browser(self) -> None:
        for funzione in ("renderCompilazione", "renderCompilazioniPrecedenti", "compilazioneMeta"):
            corpo = self.body(funzione)
            for vietato in (".reduce(", "+=", "* "):
                self.assertNotIn(vietato, corpo, f"{funzione} sembra ricalcolare un numero del servizio")
        corpo = self.body("renderCompilazione")
        # formatEuro(null) stamperebbe "0,00 €": un totale che non c'e' non e' uno zero.
        self.assertIn("totale == null", corpo)
        self.assertIn("fornitore?.totaleNetto == null", corpo)

    def test_ogni_testo_del_servizio_passa_da_escapeHtml(self) -> None:
        """I nomi dei file contengono spazi e un em dash, e prima o poi
        conterranno un apostrofo o una virgoletta: quello che il servizio manda
        non e' HTML e non deve poterlo diventare."""

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
        # Uno stato che la tabella non conosce si mostra com'e' arrivato: una
        # traduzione inventata nasconderebbe un cambio del contratto.
        self.assertIn("STATI_COMPILAZIONE[stato] || stato", self.body("compilazioneMeta"))

    # --- Il foglio di stile -------------------------------------------------

    def test_le_classi_della_consegna_hanno_uno_stile(self) -> None:
        for classe in CLASSI_DELLA_CONSEGNA:
            self.assertIn(classe, self.app_js, f"nessuno scrive la classe {classe}")
            self.assertIn(f".{classe}", self.css, f"la classe {classe} non ha nessuna regola in styles.css")

    # --- Attrezzi -----------------------------------------------------------

    def riga_con(self, corpo: str, testo: str) -> str:
        righe = [riga for riga in corpo.splitlines() if testo in riga]
        self.assertEqual(len(righe), 1, f"«{testo}» compare {len(righe)} volte, ne serve una sola")
        return righe[0]


if __name__ == "__main__":
    unittest.main()
