"""Due difetti della revisione del 6 settembre 2026, dal lato della pagina.

Prove **eseguite**: `app.js` gira davvero dentro Node, sul banco di
`test_interfaccia_pagina1` — lo stesso DOM finto e lo stesso `fetch` che
registra le chiamate. Si guarda che cosa la pagina MANDA al servizio, non che
cosa c'e' scritto nel sorgente: una prova che cerca una sottostringa sopravvive
alla mutazione che toglie il comportamento, ed e' precisamente la trappola del
§12 di `Lavori aperti` — il corpo di una funzione estratto a mano si puo'
troncare in silenzio, e da li' in poi ogni `assertNotIn` passa perche' il testo
non c'e' piu'.

* **R1** — l'abbinamento a mano manda la run del confronto. `productId` e
  `sourceRow` sono posizionali: su un confronto rifatto indicano altri due
  articoli, e il servizio non ha modo di accorgersene se la pagina non gli dice
  su quale confronto ha premuto l'utente.
* **Il salvataggio prima della rilettura** — «È questo» rilegge il confronto
  dal servizio e sostituisce quello in pagina: prima si salva, o la quantità
  scritta un momento fa sparisce senza che nessuno lo dica. Lo stesso vale per
  le altre cinque funzioni che rileggono il confronto dopo aver cambiato lo
  stato sul servizio — sconto di testata, compilazione eliminata, listino
  eliminato, documenti caricati, colonne salvate.
* **R4** — scelta la colonna «Disponibilita», la pagina chiede anche QUALI
  valori significano disponibile e li manda nella conferma. Senza, il lettore
  parte da «disponibile» e la colonna scelta non ha nessun effetto: le righe con
  NO restano ordinabili e possono vincere il confronto.
"""

from __future__ import annotations

import json
import unittest

from test_interfaccia_pagina1 import REVISIONE, BancoDiProva


class LAbbinamentoAMano(BancoDiProva):
    """R1 — «È questo» dice anche SU QUALE confronto è stato premuto."""

    def corpo_della_richiesta(self, revisione: dict) -> dict:
        """Il corpo JSON che la pagina manda a `/api/matches/abbina`."""

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

        # `REVISIONE` porta `run.id == "R1"`: e' quella la run su cui l'utente
        # ha premuto, ed e' quella che il servizio deve poter rifiutare se nel
        # frattempo il confronto e' stato rifatto.
        self.assertEqual(corpo.get("runId"), "R1")
        # E il resto della richiesta resta quello di prima.
        self.assertEqual(corpo.get("productId"), "p1")
        self.assertEqual(corpo.get("supplierId"), "cipresso")
        self.assertEqual(corpo.get("sourceRow"), 10)

    def test_senza_run_manda_una_stringa_vuota_non_salta_il_campo(self) -> None:
        """Un confronto senza run non fa saltare il campo: lo manda vuoto.

        Il servizio distingue «non me l'hai detto» da «non coincide» solo se il
        campo c'e' sempre. Un `undefined` sparisce da `JSON.stringify` e le due
        cose tornerebbero indistinguibili.
        """

        senza_run = {**REVISIONE}
        senza_run.pop("run", None)

        self.assertEqual(self.corpo_della_richiesta(senza_run).get("runId"), "")


class LAbbinamentoSalvaPrimaDiChiedere(BancoDiProva):
    """6 settembre 2026 — «È questo» salva prima di rileggere il confronto.

    Lo stesso buco di `addCatalogProduct`, sulla rotta accanto: finito
    l'abbinamento la pagina fa `loadReview()`, e quella rilettura SOSTITUISCE il
    confronto in pagina. Una quantità scritta meno di 450 ms fa — o rimasta
    indietro perché un salvataggio è fallito e sta per riprovare — non è ancora
    sul disco, e la rilettura se la porta via: il salvataggio dopo consolida il
    numero vecchio, cioè un ordine sbagliato, senza che niente lo dica.
    """

    def esegui_labbinamento(self, salvataggio: str) -> dict:
        """Preme «È questo» con un `saveState` che risponde come dico io."""

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
        """Se il numero scritto non è sul disco, non si va a rileggere niente.

        E la frase finisce dove la finestra dei listini la fa vedere, cioè in
        `state.listino.errore`: un abbinamento che non è avvenuto e non lo dice
        è peggio del difetto che si sta correggendo.
        """

        esito = self.esegui_labbinamento("false")

        self.assertIsNone(esito["corpo"], "la richiesta è partita col salvataggio non riuscito")
        self.assertIn("non sono ancora state salvate", esito["errore"])
        # Nessun «Abbinato.» sotto gli occhi: non è successo.
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
    """R4 — la colonna da sola non dice niente: servono i suoi valori."""

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
        """Senza colonna non c'è niente da dichiarare, e chiederlo sarebbe rumore."""

        self.assertNotIn('data-schema-field="availableValues"', self.scheda())

    def test_scelta_la_colonna_la_pagina_chiede_quali_valori(self) -> None:
        html = self.scheda(colonna=5)

        self.assertIn("Valori che significano disponibile", html)
        self.assertIn('data-schema-field="availableValues"', html)
        # Il segnaposto è un esempio, non un valore predefinito: inventarne uno
        # nel codice sarebbe la regola cablata che il progetto non vuole.
        self.assertIn('placeholder="SI"', html)

    def test_quello_che_scrivi_torna_nel_campo(self) -> None:
        self.assertIn('value="SI, S"', self.scheda(colonna=5, valori="SI, S"))

    def test_la_conferma_manda_i_valori_scritti(self) -> None:
        """Dal tasto premuto al corpo della richiesta, senza tappe intermedie."""

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
        """È la dichiarazione che manca: il servizio la rifiuta, e lo dice lui.

        Se la pagina saltasse il campo, «non me l'hai detto» e «non ci sono
        valori» sarebbero la stessa cosa e nessuno dei due si potrebbe dire.
        """

        corpo = self.esegui(
            "return JSON.stringify(schemaMappingPayload());",
            preparazione=self.preparazione(colonna=5),
        )

        self.assertEqual(json.loads(corpo)["mappings"][0].get("availableValues"), "")


class LeCinqueRilettureSalvanoPrima(BancoDiProva):
    """6 settembre 2026 — le altre cinque funzioni che rileggevano il confronto.

    Stesso buco di «È questo», stessa correzione, cinque rotte diverse: sconto
    di testata, compilazione eliminata, listino eliminato, documenti caricati,
    colonne salvate. Tutte e cinque mandano una richiesta che cambia lo stato
    sul servizio e SUBITO DOPO rileggono il confronto, sostituendo quello in
    pagina: una quantità scritta meno di 450 ms fa — o rimasta indietro perché
    un salvataggio è fallito e sta per riprovare — la rilettura se la porta via,
    e il salvataggio dopo consolida il numero vecchio. Cioè un ordine sbagliato,
    senza che niente lo dica.

    Prove **eseguite**: `saveState` viene sostituito nel contesto (le sue tre
    strade — niente da salvare, salvato, fallito — qui non interessano: conta
    solo che cosa risponde) e si guarda se la richiesta PARTE. Una prova che
    cercasse la guardia nel sorgente passerebbe anche con la guardia messa dopo
    la richiesta, che è esattamente il modo di sbagliare questa correzione.
    """

    def esegui_azione(self, azione: str, *, salvataggio: str, risposte: dict | None = None) -> dict:
        """Fa girare `azione` con un `saveState` che risponde come dico io."""

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
        """Il corpo JSON mandato a `rotta`, o `None` se non è partito niente."""

        for indirizzo, corpo in zip(esito["rotte"], esito["corpi"]):
            if rotta in indirizzo:
                return json.loads(corpo) if corpo else {}
        return None

    # -- Lo sconto di testata di un fornitore -------------------------------

    SCONTO = 'await applicaScontoFornitore("cipresso", 10);'
    RISPOSTE_SCONTO = {"/api/suppliers/discount": {"ok": True, "reassigned": 0}}

    def test_lo_sconto_col_salvataggio_fallito_non_parte(self) -> None:
        """Lo sconto riassegna i prodotti e la pagina rilegge tutto: prima si salva."""

        esito = self.esegui_azione(self.SCONTO, salvataggio="false", risposte=self.RISPOSTE_SCONTO)

        self.assertIsNone(self.corpo(esito, "/api/suppliers/discount"),
                          "lo sconto è partito col salvataggio non riuscito")
        # La funzione dice le sue cose col messaggio a scomparsa: è lì che
        # l'utente deve leggere perché lo sconto non è stato applicato.
        self.assertTrue(any("non sono ancora state salvate" in avviso for avviso in esito["avvisi"]),
                        esito["avvisi"])

    def test_lo_sconto_col_salvataggio_riuscito_parte_come_prima(self) -> None:
        esito = self.esegui_azione(self.SCONTO, salvataggio="true", risposte=self.RISPOSTE_SCONTO)

        self.assertEqual(self.corpo(esito, "/api/suppliers/discount"), {"supplierId": "cipresso", "percent": 10})

    # -- La compilazione eliminata dallo storico ----------------------------

    COMPILAZIONE = 'await deleteCompilation("2026-08-10 lunedì");'
    RISPOSTE_COMPILAZIONE = {
        "/api/ordini/elimina": {"ok": True, "compilazioni": [], "promemoriaRimossi": 0},
        "/api/history/pending": {"pending": []},
    }

    def test_la_compilazione_col_salvataggio_fallito_non_si_elimina(self) -> None:
        esito = self.esegui_azione(self.COMPILAZIONE, salvataggio="false", risposte=self.RISPOSTE_COMPILAZIONE)

        self.assertIsNone(self.corpo(esito, "/api/ordini/elimina"),
                          "la compilazione è stata eliminata col salvataggio non riuscito")
        # Il riquadro dello storico ha un campo suo, ed è lì che la frase va.
        self.assertIn("non sono ancora state salvate", esito["errori"]["compilazioni"])

    def test_la_compilazione_col_salvataggio_riuscito_si_elimina_come_prima(self) -> None:
        esito = self.esegui_azione(self.COMPILAZIONE, salvataggio="true", risposte=self.RISPOSTE_COMPILAZIONE)

        self.assertEqual(self.corpo(esito, "/api/ordini/elimina"), {"cartella": "2026-08-10 lunedì"})
        self.assertEqual(esito["errori"]["compilazioni"], "")

    # -- Il listino caricato, tolto dai documenti ---------------------------

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

    # -- I documenti caricati ----------------------------------------------

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
        # E il messaggio di riuscita non compare: non è successo niente.
        self.assertEqual(esito["messaggioDelCaricamento"], "")

    def test_i_documenti_col_salvataggio_riuscito_partono_come_prima(self) -> None:
        esito = self.esegui_azione(self.CARICAMENTO, salvataggio="true", risposte=self.RISPOSTE_CARICAMENTO)

        corpo = self.corpo(esito, "/api/upload")
        self.assertIsNotNone(corpo, "i documenti non sono partiti")
        self.assertEqual(corpo["files"][0]["name"], "LISTINO NUOVO.xlsx")
        self.assertEqual(corpo["files"][0]["role"], "suppliers")
        self.assertEqual(esito["messaggioDelCaricamento"], "1 documento caricato.")

    # -- Le colonne di un documento, salvate --------------------------------

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
        # La schermata della mappatura ha il suo campo d'errore: la frase va lì,
        # sotto gli occhi di chi ha appena premuto Conferma.
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
