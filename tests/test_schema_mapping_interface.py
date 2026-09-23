"""Il flusso schemi deve esistere nella pagina, non nei file di servizio."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SchemaMappingInterfaceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        cls.css = (ROOT / "app" / "static" / "styles.css").read_text(encoding="utf-8")

    def body(self, name: str) -> str:
        marker = f"function {name}("
        self.assertIn(marker, self.js)
        return self.js.split(marker, 1)[1].split("\n}\n", 1)[0]

    def test_la_pagina_usa_le_tre_rotte_guidate(self) -> None:
        api = self.js.split("const API = {", 1)[1].split("};", 1)[0]
        for route in ("/api/schemas/pending", "/api/schemas/validate", "/api/schemas/confirm"):
            self.assertIn(route, api)

    def test_il_blocco_compare_solo_per_la_fermata_giusta(self) -> None:
        self.assertIn('=== "SCHEMA_SCONOSCIUTO"', self.body("schemaMappingRequired"))
        self.assertIn("schemaMappingRequired(stato)", self.body("renderAvanzamentoPipeline"))

    def test_l_utente_vede_anteprima_e_campi_essenziali(self) -> None:
        pagina = self.body("renderSchemaDocument")
        self.assertIn("renderSchemaPreview(documento)", pagina)
        for label in (
            "Fornitore", "Foglio", "Riga delle intestazioni",
            "Nome prodotto", "Codice EAN", "Prezzo netto", "Pezzi per collo",
            "Colonna in cui scrivere l’ordine",
        ):
            self.assertIn(label, pagina)
        # ⚠ Dal 17 agosto 2026 «Prima riga dei prodotti» non sta più qui dentro:
        # sta in `renderSchemaDataStart`, insieme alla regola che la detta.  Il
        # campo dev'esserci lo stesso, e il blocco dev'essere richiamato — una
        # funzione che nessuno chiama supera qualunque ricerca di sottostringhe.
        self.assertIn("renderSchemaDataStart(documento)", pagina)
        self.assertIn("Prima riga dei prodotti", self.body("renderSchemaDataStart"))

    def test_le_colonne_secondarie_stanno_in_un_menu_a_scomparsa(self) -> None:
        pagina = self.body("renderSchemaDocument")
        # ⚠ Dal 15 agosto 2026 il riquadro ricorda di essere aperto: si
        # richiudeva a ogni scelta di colonna, cioe' proprio mentre lo si
        # compilava. La chiave porta dentro il documento — due listini aperti
        # insieme sono due riquadri diversi.
        self.assertIn('<details class="schema-optional" ${apribile(`altre-colonne:${documento.profileId}`)}>', pagina)
        self.assertIn("Altre colonne", pagina)
        self.assertIn("Codice fornitore", pagina)
        # ⚠ Dal 6 settembre 2026 «Disponibilità» non sta più qui dentro: sta in
        # `renderDisponibilita`, insieme al campo con cui si dichiara quali
        # valori di quella colonna significano disponibile. Come per «Prima riga
        # dei prodotti» qui sopra, si pretendono tutt'e due — il campo e la
        # chiamata: una funzione che nessuno chiama supera qualunque ricerca di
        # sottostringhe.
        self.assertIn("renderDisponibilita(documento, valore)", pagina)
        self.assertIn("Disponibilità", self.body("renderDisponibilita"))

    def test_conferma_e_disponibile_solo_dopo_la_verifica(self) -> None:
        pagina = self.body("renderSchemaMappingWizard")
        self.assertIn('data-action="validate-schemas"', pagina)
        self.assertIn('data-action="confirm-schemas"', pagina)
        # ⚠ Questa prova pretendeva il contrario: `!tuttiControllati` sul
        # pulsante di conferma, cioe' la prova obbligatoria prima di
        # confermare.  Era un cancello inventato dalla pagina —
        # `conferma_schemi` esegue gia' la stessa `valida_mappature`, con gli
        # stessi argomenti, prima di salvare — e costava all'utente un
        # passaggio che il servizio non chiede.  La prova resta, facoltativa.
        conferma = pagina.split('data-action="confirm-schemas"')[1].split(">")[0]
        self.assertNotIn("tuttiControllati", conferma)

    def test_la_pagina_non_chiede_json_o_comandi(self) -> None:
        self.assertNotIn("decisioni_schemi.json", self.js)
        self.assertNotIn("impara_adattatore.py", self.js)

    def test_cambiare_i_documenti_spegne_l_anteprima_della_run_precedente(self) -> None:
        self.assertIn("applyPipelineAfterInputChange(result.pipeline)", self.body("deleteUploadedList"))
        self.assertIn("applyPipelineAfterInputChange(result.pipeline)", self.body("uploadFiles"))
        aggiornamento = self.body("applyPipelineAfterInputChange")
        self.assertIn("resetSchemaMapping()", aggiornamento)
        self.assertIn('stato !== "IN_ATTESA"', aggiornamento)

    def test_tutti_i_blocchi_del_wizard_hanno_uno_stile(self) -> None:
        for classe in (
            "schema-mapping", "schema-document", "schema-routing", "schema-fields",
            "schema-preview", "schema-optional", "schema-validation", "schema-mapping__actions",
        ):
            self.assertIn(f".{classe}", self.css)


class OgniCampoDellaProceduraGuidataHaUnNome(unittest.TestCase):
    """Rilievo [22] dell'onda 5 — dodici etichette senza `for`, zero `id`.

    La procedura si apre quando un listino ha colonne che il programma non
    riconosce, cioe' quando arriva un fornitore nuovo. Nessuna etichetta aveva
    `for`, nessun campo aveva `id`, e nessuna etichetta avvolgeva il proprio
    campo: per un lettore di schermo quelle tendine non avevano nome, e si
    perdeva anche col mouse, perche' cliccare l'etichetta non portava al campo.
    `renderSchemaColumnSelect()` da sola e' richiamata quindici volte.

    Prova a sorgente sulla regione della procedura: qui non c'e' un browser.
    """

    @classmethod
    def setUpClass(cls) -> None:
        sorgente = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        inizio = sorgente.index("function idCampoSchema")
        fine = sorgente.index("function renderAvanzamentoPipeline")
        cls.regione = sorgente[inizio:fine]

    def test_nessuna_etichetta_resta_senza_for(self) -> None:
        etichette = re.findall(r"<label[^>]*>", self.regione)

        # ⚠ Tredici dal 6 settembre 2026: «Valori che significano disponibile»,
        # il campo senza cui la colonna «Disponibilità» scelta non ha nessun
        # effetto. Il numero si alza quando un campo nasce davvero, non per
        # far tornare i conti.
        self.assertEqual(len(etichette), 13)
        self.assertEqual([voce for voce in etichette if "for=" not in voce], [])

    def test_nessun_campo_resta_senza_id(self) -> None:
        campi = re.findall(r"<(?:select|input)[^>]*>", self.regione)

        # ⚠ Tredici dal 6 settembre 2026: «Valori che significano disponibile»,
        # il campo senza cui la colonna «Disponibilità» scelta non ha nessun
        # effetto. Il numero si alza quando un campo nasce davvero, non per
        # far tornare i conti.
        self.assertEqual(len(campi), 13)
        self.assertEqual([voce for voce in campi if "id=" not in voce], [])

    def test_ogni_for_punta_all_id_del_suo_campo(self) -> None:
        """⚠ La prova che conta, e che le due qui sopra non fanno: un `for` che
        punta a un id inesistente lascia il campo senza nome per il lettore di
        schermo, e il clic sull'etichetta non porta da nessuna parte. Contare
        `for=` e `id=` separatamente non se ne accorge — verificato scrivendo
        `for="X-etichetta"` su un campo `id="X"`: restavano verdi tutt'e due."""

        campi = re.findall(
            r'<label for="([^"]+)"[^>]*>.*?<(?:select|input) id="([^"]+)"',
            self.regione,
            flags=re.S,
        )
        # Le tredici coppie, e nessuna scompagnata.
        self.assertEqual(len(campi), 13, campi)
        scompagnate = [(f, i) for f, i in campi if f != i]
        self.assertEqual(scompagnate, [])

    def test_l_id_comincia_per_lettera_e_non_per_cifra(self) -> None:
        """⚠ `profileId` puo' cominciare per una cifra, e un id che comincia per
        cifra e' valido in HTML5 ma rompe i selettori CSS non scappati."""

        self.assertIn("return `c-${profileId}-${campo}`;", self.regione)

    def test_l_id_si_costruisce_coi_valori_che_stanno_gia_nel_markup(self) -> None:
        """Niente contatori ne' valori nuovi: gli stessi due che finiscono in
        `data-schema-document` e `data-schema-column`, cosi' l'id e' stabile fra
        un ridisegno e l'altro."""

        self.assertIn("function idCampoSchema(profileId, campo)", self.regione)
        self.assertNotIn("Math.random", self.regione)


if __name__ == "__main__":
    unittest.main()
