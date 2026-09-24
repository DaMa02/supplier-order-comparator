"""The schema-mapping wizard must live in the page markup itself, not only in the backend."""

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
        # "Prima riga dei prodotti" lives in `renderSchemaDataStart`, alongside the
        # rule that derives it. Both the field and the call must be checked: a
        # substring match alone wouldn't catch a function that's never invoked.
        self.assertIn("renderSchemaDataStart(documento)", pagina)
        self.assertIn("Prima riga dei prodotti", self.body("renderSchemaDataStart"))

    def test_le_colonne_secondarie_stanno_in_un_menu_a_scomparsa(self) -> None:
        pagina = self.body("renderSchemaDocument")
        # The expand/collapse state must remember it was open, otherwise the panel
        # collapses on every column pick, mid-edit. The key is scoped to the
        # document's `profileId`, so two open price lists keep separate panels.
        self.assertIn('<details class="schema-optional" ${apribile(`altre-colonne:${documento.profileId}`)}>', pagina)
        self.assertIn("Altre colonne", pagina)
        self.assertIn("Codice fornitore", pagina)
        # "Disponibilità" lives in `renderDisponibilita`, alongside the field that
        # declares which values of that column count as available. As above, both
        # the field and the call must be checked: a substring match alone wouldn't
        # catch a function that's never invoked.
        self.assertIn("renderDisponibilita(documento, valore)", pagina)
        self.assertIn("Disponibilità", self.body("renderDisponibilita"))

    def test_conferma_e_disponibile_solo_dopo_la_verifica(self) -> None:
        pagina = self.body("renderSchemaMappingWizard")
        self.assertIn('data-action="validate-schemas"', pagina)
        self.assertIn('data-action="confirm-schemas"', pagina)
        # The confirm button must NOT gate on `!tuttiControllati`: `conferma_schemi`
        # already runs the same `valida_mappature` with the same arguments before
        # saving, so a client-side gate would only add a redundant required step.
        # Validation stays available, just optional.
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
    """Every field of the schema-mapping wizard must be reachable by a screen reader.

    The wizard opens when a price list has columns the program doesn't recognise,
    i.e. a new supplier. Each select/input needs an `id`, and its `<label>` needs a
    matching `for`, otherwise the field has no accessible name and clicking the
    label doesn't focus it. `renderSchemaColumnSelect()` alone is called many times,
    so these checks run on the source region, without a browser.
    """

    @classmethod
    def setUpClass(cls) -> None:
        sorgente = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        inizio = sorgente.index("function idCampoSchema")
        fine = sorgente.index("function renderAvanzamentoPipeline")
        cls.regione = sorgente[inizio:fine]

    def test_nessuna_etichetta_resta_senza_for(self) -> None:
        etichette = re.findall(r"<label[^>]*>", self.regione)

        # This count includes "Valori che significano disponibile", the field that
        # makes the "Disponibilità" column selection actually take effect. Raise it
        # only when a field is genuinely added, not to make the assertion pass.
        self.assertEqual(len(etichette), 13)
        self.assertEqual([voce for voce in etichette if "for=" not in voce], [])

    def test_nessun_campo_resta_senza_id(self) -> None:
        campi = re.findall(r"<(?:select|input)[^>]*>", self.regione)

        # This count includes "Valori che significano disponibile", the field that
        # makes the "Disponibilità" column selection actually take effect. Raise it
        # only when a field is genuinely added, not to make the assertion pass.
        self.assertEqual(len(campi), 13)
        self.assertEqual([voce for voce in campi if "id=" not in voce], [])

    def test_ogni_for_punta_all_id_del_suo_campo(self) -> None:
        """The test the two above don't cover: a `for` pointing at a nonexistent
        id still leaves the field unnamed for a screen reader, and clicking the
        label goes nowhere. Counting `for=` and `id=` separately misses this —
        a `for="X-etichetta"` on a field `id="X"` passed both those checks."""

        campi = re.findall(
            r'<label for="([^"]+)"[^>]*>.*?<(?:select|input) id="([^"]+)"',
            self.regione,
            flags=re.S,
        )
        # All thirteen pairs, none mismatched.
        self.assertEqual(len(campi), 13, campi)
        scompagnate = [(f, i) for f, i in campi if f != i]
        self.assertEqual(scompagnate, [])

    def test_l_id_comincia_per_lettera_e_non_per_cifra(self) -> None:
        """`profileId` can start with a digit; an id starting with a digit is valid
        HTML5 but breaks unescaped CSS selectors, hence the `c-` prefix."""

        self.assertIn("return `c-${profileId}-${campo}`;", self.regione)

    def test_l_id_si_costruisce_coi_valori_che_stanno_gia_nel_markup(self) -> None:
        """No counters or new values: the same two that already end up in
        `data-schema-document` and `data-schema-column`, so the id stays stable
        across re-renders."""

        self.assertIn("function idCampoSchema(profileId, campo)", self.regione)
        self.assertNotIn("Math.random", self.regione)


if __name__ == "__main__":
    unittest.main()
