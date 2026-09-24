"""Client-side behavior of the review page, exercised end to end.

`app.js` runs for real inside Node, on the harness from
`test_interfaccia_pagina1` — the same fake DOM and the same `fetch` that
records calls. These tests check what the page SENDS to the service, not
what the source contains: a test that greps for a substring survives a
mutation that removes the behavior, since a hand-extracted function body can
be silently truncated and every `assertNotIn` would then pass simply
because the text is gone.

* Manual matching sends the comparison's run id. `productId` and
  `sourceRow` are positional: after the comparison is recomputed they can
  point at two different items, and the service has no way to notice unless
  the page also tells it which run the user was looking at.
* Saving before reloading — "È questo" reloads the comparison from the
  service and replaces the one in the page; without saving first, a
  quantity written a moment ago disappears with nothing saying so. The same
  applies to the five other actions that reload the comparison after
  changing state on the service: supplier discount, deleted compilation,
  deleted price list, uploaded documents, saved columns.
* Once the "Disponibilita" column is picked, the page also asks WHICH
  values mean available and sends them with the confirmation. Without that,
  the reader defaults to "disponibile" and the chosen column has no effect:
  rows marked NO stay orderable and can win the comparison.
"""

from __future__ import annotations

import json
import unittest

from test_interfaccia_pagina1 import REVISIONE, BancoDiProva


class LAbbinamentoAMano(BancoDiProva):
    """"È questo" also states WHICH comparison it was pressed on."""

    def corpo_della_richiesta(self, revisione: dict) -> dict:
        """The JSON body the page sends to `/api/matches/abbina`."""

        corpo = self.esegui(
            """
              state.review = normalizeReview(REVISIONE_DI_PROVA);
              state.loading = false;
              state.listino.aperto = true;
              state.listino.prodottoId = "p1";
              state.listino.fornitore = "cipresso";
              await abbinaLaRiga(10);
              const chiamata = chiamate.find((voce) => voce.indirizzo.includes("/api/matches/abbina"));
              return chiamata ? chiamata.opzioni.body : null;
            """,
            preparazione=f"const REVISIONE_DI_PROVA = {json.dumps(revisione)};",
            risposte={
                "/api/matches/abbina": {"ok": True, "message": "Abbinato."},
                "/api/review": revisione,
            },
        )
        self.assertIsNotNone(corpo, "la pagina non ha chiamato /api/matches/abbina")
        return json.loads(corpo)

    def test_labbinamento_manda_la_run_del_confronto(self) -> None:
        corpo = self.corpo_della_richiesta(REVISIONE)

        # `REVISIONE` carries `run.id == "R1"`: that's the run the user acted
        # on, and the service must be able to reject it if the comparison was
        # recomputed in the meantime.
        self.assertEqual(corpo.get("runId"), "R1")
        # The rest of the request is unchanged.
        self.assertEqual(corpo.get("productId"), "p1")
        self.assertEqual(corpo.get("supplierId"), "cipresso")
        self.assertEqual(corpo.get("sourceRow"), 10)

    def test_senza_run_manda_una_stringa_vuota_non_salta_il_campo(self) -> None:
        """A comparison with no run doesn't drop the field: it sends it empty.

        The service can only tell "you didn't say" apart from "doesn't
        match" if the field is always present. An `undefined` value
        vanishes from `JSON.stringify`, which would make the two
        indistinguishable.
        """

        senza_run = {**REVISIONE}
        senza_run.pop("run", None)

        self.assertEqual(self.corpo_della_richiesta(senza_run).get("runId"), "")


class LAbbinamentoSalvaPrimaDiChiedere(BancoDiProva):
    """"È questo" saves before reloading the comparison.

    Same gap as `addCatalogProduct` on the neighboring route: once matching
    finishes, the page calls `loadReview()`, and that reload REPLACES the
    comparison in the page. A quantity written less than 450 ms ago — or
    still pending because a save failed and is about to retry — isn't on
    disk yet, and the reload discards it; the next save then writes back the
    old number, i.e. a wrong order, with nothing pointing it out.
    """

    def esegui_labbinamento(self, salvataggio: str) -> dict:
        """Presses "È questo" with a `saveState` that returns a controlled result."""

        esito = self.esegui(
            f"""
              state.review = normalizeReview(REVISIONE_DI_PROVA);
              state.loading = false;
              state.listino.aperto = true;
              state.listino.prodottoId = "p1";
              state.listino.fornitore = "cipresso";
              // Il salvataggio vero ha tre strade (niente da salvare, salvato,
              // fallito) e qui interessa solo che cosa risponde: si sostituisce
              // la funzione, non il servizio.
              saveState = async () => {salvataggio};
              await abbinaLaRiga(10);
              const chiamata = chiamate.find((voce) => voce.indirizzo.includes("/api/matches/abbina"));
              return JSON.stringify({{
                corpo: chiamata ? chiamata.opzioni.body : null,
                errore: state.listino.errore,
                esito: state.listino.esito,
              }});
            """,
            preparazione=f"const REVISIONE_DI_PROVA = {json.dumps(REVISIONE)};",
            risposte={
                "/api/matches/abbina": {"ok": True, "message": "Abbinato."},
                "/api/review": REVISIONE,
            },
        )
        return json.loads(esito)

    def test_col_salvataggio_fallito_la_richiesta_non_parte(self) -> None:
        """If the written number isn't on disk yet, nothing gets reloaded.

        The message lands in `state.listino.errore`, where the price-list
        window shows it: a match that didn't happen and doesn't say so
        would be worse than the bug this fixes.
        """

        esito = self.esegui_labbinamento("false")

        self.assertIsNone(esito["corpo"], "la richiesta è partita col salvataggio non riuscito")
        self.assertIn("non sono ancora state salvate", esito["errore"])
        # No "Abbinato." shown: it didn't happen.
        self.assertEqual(esito["esito"], "")

    def test_col_salvataggio_riuscito_la_richiesta_parte_come_prima(self) -> None:
        esito = self.esegui_labbinamento("true")

        corpo = json.loads(esito["corpo"])
        self.assertEqual(corpo.get("runId"), "R1")
        self.assertEqual(corpo.get("productId"), "p1")
        self.assertEqual(corpo.get("supplierId"), "cipresso")
        self.assertEqual(corpo.get("sourceRow"), 10)
        self.assertEqual(esito["errore"], "")
        self.assertEqual(esito["esito"], "Abbinato.")


class LaColonnaDisponibilita(BancoDiProva):
    """The column alone says nothing: its values also need declaring."""

    DOCUMENTO = {
        "profileId": "d1",
        "fileName": "LISTINO NUOVO.xlsx",
        "format": "xlsx",
        "sheets": [{"name": "Foglio1", "maxColumn": 8, "maxRow": 200, "headerRows": [1]}],
        "reason": {"state": "SCONOSCIUTO"},
    }

    def preparazione(self, *, colonna: int | None = None, valori: str = "") -> str:
        valore = {
            "role": "supplier", "supplierChoice": "__new__", "sheet": "Foglio1",
            "headerRow": 1, "dataStartRow": 2, "factorMode": "fixed",
            "piecesPerCartonDefault": 1, "orderColumn": "", "commercialLayout": "",
            "availableValues": valori,
            "columns": {"description": 1, "unit_price_net": 2} | ({"availability": colonna} if colonna else {}),
        }
        return f"""
          state.loading = false;
          state.schemaMapping.data = {{
            runId: "R1",
            documents: [{json.dumps(self.DOCUMENTO)}],
            suppliers: [],
          }};
          state.schemaMapping.loadedRunId = "R1";
          state.schemaMapping.values = {{ d1: {json.dumps(valore)} }};
        """

    def scheda(self, **kwargs) -> str:
        return self.esegui(
            "return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(**kwargs),
        )

    def test_senza_la_colonna_il_campo_non_c_e(self) -> None:
        """Without a column there's nothing to declare, and asking would be noise."""

        self.assertNotIn('data-schema-field="availableValues"', self.scheda())

    def test_scelta_la_colonna_la_pagina_chiede_quali_valori(self) -> None:
        html = self.scheda(colonna=5)

        self.assertIn("Valori che significano disponibile", html)
        self.assertIn('data-schema-field="availableValues"', html)
        # The placeholder is an example, not a default value: hardcoding one
        # would reintroduce the fixed rule this project avoids.
        self.assertIn('placeholder="SI"', html)

    def test_quello_che_scrivi_torna_nel_campo(self) -> None:
        self.assertIn('value="SI, S"', self.scheda(colonna=5, valori="SI, S"))

    def test_la_conferma_manda_i_valori_scritti(self) -> None:
        """From the button press to the request body, with no intermediate steps."""

        corpo = self.esegui(
            """
              cambia({ schemaDocument: "d1", schemaField: "availableValues" }, { value: "SI, S" });
              return JSON.stringify(schemaMappingPayload());
            """,
            preparazione=self.preparazione(colonna=5),
        )

        mappatura = json.loads(corpo)["mappings"][0]
        self.assertEqual(mappatura.get("availableValues"), "SI, S")

    def test_senza_valori_il_campo_viaggia_lo_stesso_vuoto(self) -> None:
        """The declaration is missing, and it's the service's job to reject it, not the page's.

        If the page dropped the field, "you didn't say" and "there are no
        values" would collapse into the same thing, and neither could be told apart.
        """

        corpo = self.esegui(
            "return JSON.stringify(schemaMappingPayload());",
            preparazione=self.preparazione(colonna=5),
        )

        self.assertEqual(json.loads(corpo)["mappings"][0].get("availableValues"), "")


class LeCinqueRilettureSalvanoPrima(BancoDiProva):
    """The other five actions that reload the comparison after a state change.

    Same gap as "È questo", same fix, five different routes: supplier
    discount, deleted compilation, deleted price list, uploaded documents,
    saved columns. All five send a request that changes state on the
    service and immediately reload the comparison, replacing the one in the
    page: a quantity written less than 450 ms ago — or still pending
    because a save failed and is about to retry — gets discarded by the
    reload, and the next save then writes back the old number: a wrong
    order, with nothing pointing it out.

    `saveState` is stubbed in the page context (its three outcomes —
    nothing to save, saved, failed — don't matter here, only the return
    value does), and each test checks whether the request FIRES. A test
    that grepped the source for the guard would also pass with the guard
    placed after the request, which is exactly the wrong way to fix this.
    """

    def esegui_azione(self, azione: str, *, salvataggio: str, risposte: dict | None = None) -> dict:
        """Runs `azione` with a `saveState` that returns a controlled result."""

        esito = self.esegui(
            f"""
              // Prima si lascia finire l'avvio della pagina — `loadReview()` in
              // fondo ad app.js — altrimenti la sua risposta arriverebbe in
              // mezzo all'azione e riazzererebbe i campi che qui si guardano.
              await attendi(10);
              const avvisi = [];
              nodoDi("#toast-region").append = (nodo) => avvisi.push(String(nodo.textContent || ""));
              saveState = async () => {salvataggio};
              {azione}
              return JSON.stringify({{
                rotte: chiamate.map((voce) => voce.indirizzo),
                corpi: chiamate.map((voce) => String((voce.opzioni || {{}}).body || "")),
                avvisi,
                errori: {{
                  runtime: state.runtimeError,
                  compilazioni: state.compilazioni.errore,
                  colonne: state.schemaMapping.error,
                }},
                messaggioDelCaricamento: state.uploadMessage,
              }});
            """,
            preparazione=f"const REVISIONE_DI_PROVA = {json.dumps(REVISIONE)};",
            risposte={"/api/review": REVISIONE, **(risposte or {})},
        )
        return json.loads(esito)

    def corpo(self, esito: dict, rotta: str):
        """The JSON body sent to `rotta`, or `None` if nothing was sent."""

        for indirizzo, corpo in zip(esito["rotte"], esito["corpi"]):
            if rotta in indirizzo:
                return json.loads(corpo) if corpo else {}
        return None

    # -- Supplier discount ---------------------------------------------------

    SCONTO = 'await applicaScontoFornitore("cipresso", 10);'
    RISPOSTE_SCONTO = {"/api/suppliers/discount": {"ok": True, "reassigned": 0}}

    def test_lo_sconto_col_salvataggio_fallito_non_parte(self) -> None:
        """The discount reassigns products and the page reloads everything: save must happen first."""

        esito = self.esegui_azione(self.SCONTO, salvataggio="false", risposte=self.RISPOSTE_SCONTO)

        self.assertIsNone(self.corpo(esito, "/api/suppliers/discount"),
                          "lo sconto è partito col salvataggio non riuscito")
        # The toast message is where the user reads why the discount wasn't applied.
        self.assertTrue(any("non sono ancora state salvate" in avviso for avviso in esito["avvisi"]),
                        esito["avvisi"])

    def test_lo_sconto_col_salvataggio_riuscito_parte_come_prima(self) -> None:
        esito = self.esegui_azione(self.SCONTO, salvataggio="true", risposte=self.RISPOSTE_SCONTO)

        self.assertEqual(self.corpo(esito, "/api/suppliers/discount"), {"supplierId": "cipresso", "percent": 10})

    # -- Compilation deleted from history ------------------------------------

    COMPILAZIONE = 'await deleteCompilation("2026-08-10 lunedì");'
    RISPOSTE_COMPILAZIONE = {
        "/api/ordini/elimina": {"ok": True, "compilazioni": [], "promemoriaRimossi": 0},
        "/api/history/pending": {"pending": []},
    }

    def test_la_compilazione_col_salvataggio_fallito_non_si_elimina(self) -> None:
        esito = self.esegui_azione(self.COMPILAZIONE, salvataggio="false", risposte=self.RISPOSTE_COMPILAZIONE)

        self.assertIsNone(self.corpo(esito, "/api/ordini/elimina"),
                          "la compilazione è stata eliminata col salvataggio non riuscito")
        # The history panel has its own error field; the message goes there.
        self.assertIn("non sono ancora state salvate", esito["errori"]["compilazioni"])

    def test_la_compilazione_col_salvataggio_riuscito_si_elimina_come_prima(self) -> None:
        esito = self.esegui_azione(self.COMPILAZIONE, salvataggio="true", risposte=self.RISPOSTE_COMPILAZIONE)

        self.assertEqual(self.corpo(esito, "/api/ordini/elimina"), {"cartella": "2026-08-10 lunedì"})
        self.assertEqual(esito["errori"]["compilazioni"], "")

    # -- Uploaded price list removed from documents --------------------------

    LISTINO = 'await deleteUploadedList("LISTINO CIPRESSO.xlsx");'
    RISPOSTE_LISTINO = {"/api/uploads/elimina": {"ok": True, "message": "Listino eliminato.", "pipeline": {"stato": "IN_ATTESA"}}}

    def test_il_listino_col_salvataggio_fallito_non_si_elimina(self) -> None:
        esito = self.esegui_azione(self.LISTINO, salvataggio="false", risposte=self.RISPOSTE_LISTINO)

        self.assertIsNone(self.corpo(esito, "/api/uploads/elimina"),
                          "il listino è stato eliminato col salvataggio non riuscito")
        self.assertIn("non sono ancora state salvate", esito["errori"]["runtime"])

    def test_il_listino_col_salvataggio_riuscito_si_elimina_come_prima(self) -> None:
        esito = self.esegui_azione(self.LISTINO, salvataggio="true", risposte=self.RISPOSTE_LISTINO)

        self.assertEqual(self.corpo(esito, "/api/uploads/elimina"), {"name": "LISTINO CIPRESSO.xlsx"})
        self.assertEqual(esito["errori"]["runtime"], "")

    # -- Uploaded documents ---------------------------------------------------

    CARICAMENTO = """
      // Il minimo perché `uploadFiles` arrivi alla richiesta: un file scelto,
      // con quel tanto di `File` che la pagina gli chiede (`arrayBuffer`).
      state.pendingFiles = [{
        key: "k1",
        role: "suppliers",
        file: {
          name: "LISTINO NUOVO.xlsx",
          size: 3,
          lastModified: 1,
          arrayBuffer: async () => new Uint8Array([76, 73, 83]).buffer,
        },
      }];
      await uploadFiles("suppliers");
    """
    RISPOSTE_CARICAMENTO = {"/api/upload": {"ok": True, "message": "1 documento caricato.", "pipeline": {"stato": "IN_ATTESA"}}}

    def test_i_documenti_col_salvataggio_fallito_non_partono(self) -> None:
        esito = self.esegui_azione(self.CARICAMENTO, salvataggio="false", risposte=self.RISPOSTE_CARICAMENTO)

        self.assertIsNone(self.corpo(esito, "/api/upload"),
                          "i documenti sono partiti col salvataggio non riuscito")
        self.assertIn("non sono ancora state salvate", esito["errori"]["runtime"])
        # And the success message doesn't show: nothing happened.
        self.assertEqual(esito["messaggioDelCaricamento"], "")

    def test_i_documenti_col_salvataggio_riuscito_partono_come_prima(self) -> None:
        esito = self.esegui_azione(self.CARICAMENTO, salvataggio="true", risposte=self.RISPOSTE_CARICAMENTO)

        corpo = self.corpo(esito, "/api/upload")
        self.assertIsNotNone(corpo, "i documenti non sono partiti")
        self.assertEqual(corpo["files"][0]["name"], "LISTINO NUOVO.xlsx")
        self.assertEqual(corpo["files"][0]["role"], "suppliers")
        self.assertEqual(esito["messaggioDelCaricamento"], "1 documento caricato.")

    # -- A document's columns, saved ------------------------------------------

    COLONNE = """
      state.schemaMapping.data = {
        runId: "R1",
        documents: [{
          profileId: "d1",
          fileName: "LISTINO NUOVO.xlsx",
          format: "xlsx",
          sheets: [{ name: "Foglio1", maxColumn: 8, maxRow: 200, headerRows: [1] }],
        }],
        suppliers: [],
      };
      state.schemaMapping.loadedRunId = "R1";
      state.schemaMapping.documento = "LISTINO NUOVO.xlsx";
      state.schemaMapping.values = { d1: {
        role: "supplier", supplierChoice: "cipresso", sheet: "Foglio1",
        headerRow: 1, dataStartRow: 2, factorMode: "fixed", piecesPerCartonDefault: 1,
        orderColumn: "", commercialLayout: "", availableValues: "",
        columns: { description: 1, unit_price_net: 2 },
      } };
      await salvaColonneDocumento();
    """
    RISPOSTE_COLONNE = {"/api/schemas/documento/salva": {"ok": True, "pipeline": {"stato": "IN_ATTESA"}}}

    def test_le_colonne_col_salvataggio_fallito_non_si_salvano(self) -> None:
        esito = self.esegui_azione(self.COLONNE, salvataggio="false", risposte=self.RISPOSTE_COLONNE)

        self.assertIsNone(self.corpo(esito, "/api/schemas/documento/salva"),
                          "le colonne sono state salvate col salvataggio non riuscito")
        # The mapping screen has its own error field; that's where the
        # message goes, right where the user just clicked Conferma.
        self.assertIn("non sono ancora state salvate", esito["errori"]["colonne"])

    def test_le_colonne_col_salvataggio_riuscito_si_salvano_come_prima(self) -> None:
        esito = self.esegui_azione(self.COLONNE, salvataggio="true", risposte=self.RISPOSTE_COLONNE)

        corpo = self.corpo(esito, "/api/schemas/documento/salva")
        self.assertIsNotNone(corpo, "le colonne non sono state salvate")
        self.assertEqual(corpo["runId"], "R1")
        self.assertEqual(corpo["fileName"], "LISTINO NUOVO.xlsx")
        self.assertEqual(corpo["mappings"][0]["profileId"], "d1")
        self.assertEqual(esito["errori"]["colonne"], "")


if __name__ == "__main__":
    unittest.main()
