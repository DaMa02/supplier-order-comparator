"""Page 1 as its users experience it: exercised for real, not grepped for.

Why. The interface tests already in the project read `app.js` as text and
search for substrings — exactly the weakness that let three regressions
survive a green suite: a generic substring survives the mutation that removes
the behavior it's meant to guard. Here `app.js` is loaded and executed in
Node — the same Node the writer uses to produce price lists, found with
`find_node_runtime()` — inside a fake DOM, and the render functions' actual
return values are checked.

What this file does NOT cover:

* no browser: no layout, no applied CSS, no real mouse or keyboard events.
  CSS classes are checked only for existing in the stylesheet, not for their
  effect;
* click handlers are actually PRESSED: the fake node registers them and
  `premi("action", {data})` runs them for real. There are still no real mouse
  or keyboard events: `premi` builds the event, the browser doesn't fire it;
* the local service is faked (`fetch` answers from a lookup table): this
  tests how the page *reacts* to a response, not that the service actually
  sends it.

If Node isn't available, the tests that need it are skipped and only the
source-level tests remain, each declared as such in its own docstring.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "app" / "static" / "app.js"
STYLES = ROOT / "app" / "static" / "styles.css"

# `app/` and `scripts/` on the path, like the other test files do. Without
# this, `percorso_node()` can't load `launcher.py` — which imports
# `versione_del_codice` — the exception lands in its `except` and Node looks
# absent: most tests in this file skip silently.
for _cartella in (ROOT / "app", ROOT / "scripts"):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))


def percorso_node() -> str | None:
    """The project's Node runtime, found the same way the writer finds it."""

    spec = importlib.util.spec_from_file_location(
        "compara_launcher_interfaccia_pagina1", ROOT / "app" / "launcher.py",
    )
    if spec is None or spec.loader is None:
        return None
    modulo = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = modulo
    try:
        spec.loader.exec_module(modulo)
        runtime = modulo.find_node_runtime()
    except Exception:  # noqa: BLE001 - skip instead of failing when Node is missing
        return None
    return str(runtime.executable) if runtime else None


# The test harness: a DOM fake enough for `app.js` to load, plus a `fetch`
# that answers from an address -> body table. The `app.js` functions stay the
# real ones: nothing here is reimplemented.
BANCO = r"""
const fs = require("node:fs");
const vm = require("node:vm");

const [, , percorsoApp] = process.argv;
let ingresso = "";
process.stdin.on("data", (pezzo) => { ingresso += pezzo; });
process.stdin.on("end", () => {
  const scrivi = (esito) => process.stdout.write(JSON.stringify(esito));
  // Il programma puo' restituire una promessa: si aspetta, altrimenti
  // `JSON.stringify` di una Promise e' `{}` e il test guarderebbe il nulla.
  Promise.resolve()
    .then(() => esegui(JSON.parse(ingresso)))
    .then((valore) => scrivi({ ok: true, valore }))
    .catch((errore) => scrivi({ ok: false, errore: String((errore && errore.stack) || errore) }));
});

function nodoFinto() {
  // ⚠ I gestori si REGISTRANO, non si buttano via.  Finche' `addEventListener`
  // era un `() => {}`, i due gestori unici di `app.js` — quello di `#stepper` e
  // quello di `#app` con i suoi ~52 rami `if (action === ...)` — non li
  // eseguiva nessuna prova: si poteva metterci `return` in testa a tutti e due
  // e restavano verdi 295 collaudi con nessun pulsante funzionante.  Adesso
  // `premi()` li ritrova qui dentro e li chiama davvero.
  const ascoltatori = new Map();
  return {
    innerHTML: "", textContent: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    ascoltatori,
    addEventListener(tipo, gestore) {
      if (!ascoltatori.has(tipo)) ascoltatori.set(tipo, []);
      ascoltatori.get(tipo).push(gestore);
    },
    removeEventListener(tipo, gestore) {
      const elenco = ascoltatori.get(tipo) || [];
      const posizione = elenco.indexOf(gestore);
      if (posizione >= 0) elenco.splice(posizione, 1);
    },
    append() {}, remove() {},
    focus() {}, blur() {}, scrollIntoView() {},
    querySelector() { return null; }, closest() { return null; }, matches() { return false; },
  };
}

function chiaveDelSelettore(selettore) {
  // `[data-upload-name]` -> `uploadName`, la stessa traduzione che il browser
  // fa da attributo a `dataset`.  ⚠ E anche la forma col valore,
  // `[data-action="chiudi-impostazioni"]`: senza, `closest()` rispondeva
  // sempre `null` e una prova che dichiarava di premere quel comando passava
  // da tutt'altra strada senza accorgersene.
  const trovato = /^\[data-([a-z-]+)(?:="(.*)")?\]$/.exec(String(selettore));
  if (!trovato) return null;
  return {
    chiave: trovato[1].replace(/-([a-z])/g, (_intero, lettera) => lettera.toUpperCase()),
    valore: trovato[2],
  };
}

function bersaglioFinto(dati) {
  // L'elemento su cui e' arrivato il clic.  `closest("[data-action]")` risponde
  // se e solo se quel dato c'e' davvero, cosi' una prova che dimentica il
  // `data-*` giusto fallisce invece di passare per caso.
  const elemento = nodoFinto();
  elemento.dataset = dati;
  const corrisponde = (selettore) => {
    const voce = chiaveDelSelettore(selettore);
    if (voce === null || dati[voce.chiave] === undefined) return false;
    return voce.valore === undefined || String(dati[voce.chiave]) === voce.valore;
  };
  elemento.closest = (selettore) => (corrisponde(selettore) ? elemento : null);
  elemento.matches = corrisponde;
  return elemento;
}

function eventoFinto(dati) {
  const elemento = bersaglioFinto(dati);
  return {
    target: elemento,
    currentTarget: elemento,
    preventDefault() {},
    stopPropagation() {},
  };
}

function rispostaFinta(corpo) {
  // Un corpo `{ "__jsonRotto": true }` simula la risposta che DICE di essere
  // JSON e non lo e' (un 200 con dentro una pagina d'errore): e' l'unico modo
  // di provare che la pagina non la scambia per un corpo vuoto.
  const rotto = Boolean(corpo && corpo.__jsonRotto);
  // `__stato: 422` simula una risposta rifiutata dal servizio: serve a provare
  // che il corpo — dentro cui stanno i controlli non superati — non si perda.
  const stato = Number((corpo && corpo.__stato) || 200);
  return {
    ok: stato < 400, status: stato, statusText: stato < 400 ? "OK" : "Errore",
    headers: { get: (nome) => (String(nome).toLowerCase() === "content-type" ? "application/json" : null) },
    json: async () => {
      if (rotto) throw new SyntaxError("Unexpected token < in JSON at position 0");
      return corpo;
    },
    text: async () => (rotto ? "<html>errore</html>" : JSON.stringify(corpo)),
  };
}

function esegui(richiesta) {
  const risposte = richiesta.risposte || {};
  // `document.querySelector` restituisce lo STESSO nodo per lo stesso
  // selettore, come fa il browser: senza, `premi()` non ritroverebbe mai i
  // gestori che `app.js` ha registrato su `#app` e su `#stepper`.
  const nodi = new Map();
  // Ogni chiamata al servizio locale finisce qui: e' cosi' che una prova che
  // preme puo' chiedere «con quale rotta e con quale corpo», non solo «e'
  // successo qualcosa».
  const chiamate = [];
  // I selettori che il banco deve dichiarare ASSENTI: senza, `querySelector`
  // crea il nodo al volo e i rami «non l'ho trovato» di `app.js` non li esegue
  // nessuno — una prova su quel ramo sarebbe verde per costruzione.
  const assenti = new Set();
  const nodo = (selettore) => {
    const chiave = String(selettore);
    if (assenti.has(chiave)) return null;
    if (!nodi.has(chiave)) nodi.set(chiave, nodoFinto());
    return nodi.get(chiave);
  };
  const scatena = (selettore, tipo, evento) => {
    const ascoltatori = nodo(selettore).ascoltatori.get(tipo) || [];
    if (ascoltatori.length === 0) {
      // Se `app.js` smettesse di registrare il gestore, le prove che premono
      // devono cadere qui e non passare in silenzio non facendo niente.
      throw new Error(`nessun gestore «${tipo}» registrato su ${selettore}`);
    }
    let ultimo;
    for (const ascoltatore of ascoltatori) ultimo = ascoltatore(evento);
    return ultimo;
  };
  const sandbox = {
    console, Intl, URL, URLSearchParams, Date, Math, JSON, Number, String, Boolean,
    Array, Object, Map, Set, Promise, Error, Uint8Array, Buffer, setTimeout, clearTimeout,
    CSS: { escape: (valore) => String(valore) },
    HTMLDetailsElement: class HTMLDetailsElement {},
    // ⚠ Era `{ getItem: () => null, setItem() {} }`: una memoria che non
    // ricorda niente non fa fallire chi la usa, ma nessuna prova poteva
    // chiedere «e dopo un ricaricamento?». Adesso e' una memoria vera, in
    // RAM, e parte vuota come quella di un browser appena aperto.
    localStorage: {
      dati: new Map(),
      getItem(chiave) { return this.dati.has(String(chiave)) ? this.dati.get(String(chiave)) : null; },
      setItem(chiave, valore) { this.dati.set(String(chiave), String(valore)); },
      removeItem(chiave) { this.dati.delete(String(chiave)); },
    },
    fetch: (indirizzo, opzioni) => {
      chiamate.push({ indirizzo: String(indirizzo), opzioni: opzioni || {} });
      const chiave = Object.keys(risposte).find((voce) => String(indirizzo).includes(voce));
      if (chiave === undefined) return Promise.reject(new Error("nessuna risposta per " + indirizzo));
      return Promise.resolve(rispostaFinta(risposte[chiave]));
    },
    document: {
      querySelector: (selettore) => nodo(selettore),
      createElement: () => nodoFinto(),
      addEventListener() {},
      activeElement: null,
    },
  };
  sandbox.globalThis = sandbox;
  // ⚠ I gestori di `window` si registrano come quelli dei nodi: finche'
  // `addEventListener` era un `() => {}`, il gestore di `keydown` — cioe' Esc
  // sulle quattro finestre modali — non lo eseguiva nessuna prova.
  const ascoltatoriFinestra = new Map();
  sandbox.window = {
    location: { search: "", origin: "http://localhost:8765" },
    // ⚠ Solo l'attesa zero fa girare davvero il suo gestore: e' quella con cui
    // `app.js` rimanda lo spostamento del fuoco a dopo l'inserimento del
    // markup, perche' `autofocus` non viene onorato sui nodi messi con
    // `innerHTML`. Le attese vere — 450 ms del salvataggio, 3.600 ms dei
    // messaggi, il ritmo del ricalcolo — restano finte, altrimenti ogni prova
    // dovrebbe aspettarle davvero.
    setTimeout: (gestore, attesa) => {
      if (!attesa && typeof gestore === "function") gestore();
      return 0;
    },
    clearTimeout() {},
    addEventListener(tipo, gestore) {
      if (!ascoltatoriFinestra.has(tipo)) ascoltatoriFinestra.set(tipo, []);
      ascoltatoriFinestra.get(tipo).push(gestore);
    },
    scrollTo() {},
    confirm: () => true,
    btoa: (testo) => Buffer.from(testo, "binary").toString("base64"),
  };
  // Il banco che PREME.  `premi("next")` costruisce l'evento che il gestore di
  // `#app` si aspetta e glielo passa: da qui in poi una prova puo' chiedere
  // «premendo questo pulsante, che cosa succede», non solo «il pulsante c'e'».
  sandbox.chiamate = chiamate;
  sandbox.premi = (azione, dati) => scatena("#app", "click", eventoFinto({ action: azione, ...(dati || {}) }));
  sandbox.premiPasso = (passo, dati) => scatena("#stepper", "click", eventoFinto({ step: String(passo), ...(dati || {}) }));
  // ⚠ SENZA `step`: la quarta voce della barra — «Impostazioni», che compare
  // solo mentre sono aperte — non e' un passo del lavoro e non porta
  // `data-step`. Premerla con un `data-step` anche vuoto farebbe scattare il
  // ramo vecchio, e `goToStep()` chiude le impostazioni da solo: la prova
  // sarebbe verde anche senza la voce nuova.
  sandbox.premiNellaBarra = (dati) => scatena("#stepper", "click", eventoFinto(dati || {}));
  // Il banco che CAMBIA.  Tre controlli del percorso decisionale — quale
  // fornitore, «confermo che e' lo stesso articolo», «confermo gli ordini sotto
  // il minimo» — non si premono: si cambiano, e passano dal gestore "change".
  // `proprieta` mette sul bersaglio quello che il gestore legge: `value` per un
  // radio, `checked` per una spunta.
  sandbox.cambia = (dati, proprieta) => {
    const evento = eventoFinto(dati || {});
    Object.assign(evento.target, proprieta || {});
    return scatena("#app", "change", evento);
  };
  // Il banco che PREME UN TASTO.  `tasto("Escape")` esegue il gestore di
  // `keydown` che `app.js` registra su `window`, quello che chiude la finestra
  // modale in cima.
  sandbox.tasto = (chiave) => {
    const ascoltatori = ascoltatoriFinestra.get("keydown") || [];
    if (ascoltatori.length === 0) {
      throw new Error("nessun gestore «keydown» registrato su window");
    }
    let ultimo;
    for (const ascoltatore of ascoltatori) {
      ultimo = ascoltatore({ key: chiave, preventDefault() {}, stopPropagation() {} });
    }
    return ultimo;
  };
  // Il nodo che il banco restituisce per un selettore, per poterne contare i
  // `focus()`: `document.querySelector` risponde sempre con lo stesso.
  sandbox.nodoDi = (selettore) => nodo(selettore);
  sandbox.dichiaraAssente = (selettore) => { assenti.add(String(selettore)); };
  // Il banco che CLICCA SULLO SFONDO.  Il bersaglio non porta nessun
  // `data-action`: e' proprio il punto della correzione — lo sfondo chiude, e
  // un clic dentro la finestra che finisce su un margine no.
  sandbox.cliccaSu = (classe) => {
    const elemento = bersaglioFinto({});
    elemento.classList = { add() {}, remove() {}, contains: (voce) => voce === classe };
    return scatena("#app", "click", { target: elemento, currentTarget: elemento,
      preventDefault() {}, stopPropagation() {} });
  };
  // Il gestore non restituisce le promesse che avvia (`fetch`, salvataggi):
  // `attendi()` lascia girare qualche giro di coda perche' arrivino in fondo.
  sandbox.attendi = async (giri) => {
    for (let giro = 0; giro < (giri || 5); giro += 1) {
      await new Promise((risolvi) => setTimeout(risolvi, 0));
    }
  };
  const contesto = vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(percorsoApp, "utf8"), contesto, { filename: "app.js" });
  if (richiesta.preparazione) vm.runInContext(richiesta.preparazione, contesto, { filename: "preparazione.js" });
  // `async`: i gestori dei clic avviano `fetch` e salvataggi che non
  // restituiscono, e una prova che preme deve poter scrivere `await attendi()`
  // prima di guardare com'e' finita.
  return vm.runInContext(`(async () => { ${richiesta.programma} })()`, contesto, { filename: "programma.js" });
}
"""


# An ordinary comparison: a management-software export, one price list, one
# product with one offer. The baseline the tests vary one thing at a time from.
REVISIONE = {
    "run": {"id": "R1", "status": "ready", "createdAt": "2026-08-10T09:00:00+02:00", "label": "Confronto"},
    "files": [
        {"name": "gestionale.xlsx", "kind": "Gestionale", "supplier": "Gestionale", "status": "ready", "deletable": True},
        {"name": "LISTINO CIPRESSO.xlsx", "kind": "Listino", "supplier": "CIPRESSO", "status": "ready", "deletable": True},
    ],
    "suppliers": [
        {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 500},
        {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
    ],
    "products": [{
        "id": "p1",
        "name": "Prodotto uno",
        "quantity": 3,
        "selectedSupplierId": "cipresso",
        "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
    }],
}

CAMBIAMENTO_ELIMINATO = {
    "stato": "IN_ATTESA",
    "messaggio": "I documenti sono cambiati. Ricalcola il confronto quando sono pronti.",
    "cambiamento": {
        "tipo": "eliminato",
        "documenti": ["LISTINO CIPRESSO VALIDO FINO AL 01-09-26.xlsx"],
        "fornitori": ["CIPRESSO"],
    },
}


class BancoDiProva(unittest.TestCase):
    """Common base: runs the real `app.js` inside Node."""

    maxDiff = None

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = percorso_node()
        if not cls.node:
            raise unittest.SkipTest("Node non disponibile: le prove eseguite si saltano")
        cls._cartella = tempfile.TemporaryDirectory()
        cls.banco = Path(cls._cartella.name) / "banco.js"
        cls.banco.write_text(BANCO, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cartella = getattr(cls, "_cartella", None)
        if cartella is not None:
            cartella.cleanup()

    def esegui(self, programma: str, *, preparazione: str = "", risposte: dict | None = None,
               app_js: Path | None = None):
        richiesta = {
            "programma": programma,
            "preparazione": preparazione,
            "risposte": risposte or {},
        }
        processo = subprocess.run(
            [self.node, str(self.banco), str(app_js or APP_JS)],
            input=json.dumps(richiesta),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=90,
        )
        self.assertEqual(processo.returncode, 0, processo.stderr)
        esito = json.loads(processo.stdout)
        self.assertTrue(esito.get("ok"), esito.get("errore"))
        return esito["valore"]

    def con_confronto(self, *, pipeline: dict | None = None, pendenti: int = 0, revisione: dict | None = None,
                      passo: int = 1) -> str:
        """Set up `state` with an already-run comparison and a pipeline status."""

        return f"""
          state.review = normalizeReview({json.dumps(revisione or REVISIONE)});
          state.loading = false;
          state.currentStep = {passo};
          state.pipeline.stato = {json.dumps(pipeline)};
          state.pipeline.chiesto = Boolean({json.dumps(pipeline)} && {json.dumps(pipeline)}.stato !== "IN_ATTESA");
          for (let indice = 0; indice < {pendenti}; indice += 1) {{
            state.pendingFiles.push({{ key: "k" + indice, role: "suppliers", file: {{ name: "nuovo" + indice + ".xlsx", size: 10 }} }});
          }}
        """


class FasciaDocumentiCambiati(BancoDiProva):
    """The "documents changed" banner survives a rerender.

    Exercised tests: call the real `app.js` functions.
    """

    def test_in_attesa_senza_cambiamento_non_disegna_niente(self) -> None:
        """The original assumption still holds: without `cambiamento`, nothing fires."""

        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline={"stato": "IN_ATTESA", "messaggio": "Pronto."}),
        )
        self.assertNotIn("stale-comparison", html)
        self.assertNotIn("dopo l’ultimo confronto", html)

    def test_in_attesa_con_cambiamento_disegna_la_fascia_coi_nomi_veri(self) -> None:
        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=CAMBIAMENTO_ELIMINATO),
        )
        self.assertIn("stale-comparison", html)
        # The supplier's real name, not "a price list" and not the file name.
        self.assertIn("Hai eliminato il listino CIPRESSO dopo l’ultimo confronto.", html)
        # And the date of the comparison still being viewed.
        self.assertIn("I prezzi che vedi nelle pagine 2 e 3 sono ancora quelli di lunedì 10 agosto.", html)

    def test_la_fascia_dice_caricato_quando_e_un_caricamento(self) -> None:
        pipeline = {
            "stato": "IN_ATTESA",
            "cambiamento": {"tipo": "caricato", "documenti": ["a.xlsx", "b.xlsx"], "fornitori": ["BETULLA", "LARICE"]},
        }
        frasi = self.esegui(
            "return frasiCambiamentoDocumenti();",
            preparazione=self.con_confronto(pipeline=pipeline),
        )
        self.assertEqual(frasi["titolo"], "Hai caricato i listini BETULLA e LARICE dopo l’ultimo confronto.")

    def test_la_fascia_non_impedisce_di_continuare(self) -> None:
        """Must not block anything: the Continue button stays enabled."""

        continua = self.esegui(
            "return comandoContinua();",
            preparazione=self.con_confronto(pipeline=CAMBIAMENTO_ELIMINATO),
        )
        self.assertFalse(continua["disabilitato"])
        self.assertEqual(continua["etichetta"], "Continua sul confronto di lunedì 10 agosto")

    def test_la_fascia_sparisce_dopo_un_ricalcolo_riuscito(self) -> None:
        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline={"stato": "COMPLETATO", "messaggio": "Confronto aggiornato.", "fasi": []}),
        )
        self.assertNotIn("stale-comparison", html)

    def test_la_fascia_torna_dopo_un_ricaricamento_della_pagina(self) -> None:
        """The piece the startup code was dropping.

        Nothing is prepared by hand here: `loadReview()` and the pipeline-status
        request run exactly as they do on page load, with the local service
        answering IN_ATTESA + cambiamento.
        """

        html = self.esegui(
            """
              return (async () => {
                await new Promise((risolvi) => setTimeout(risolvi, 80));
                return { barra: renderAvanzamentoPipeline(), html: renderUploadStep() };
              })();
            """,
            risposte={"/api/review": REVISIONE, "/api/pipeline/stato": CAMBIAMENTO_ELIMINATO},
        )
        self.assertIn("stale-comparison", html["html"])
        self.assertIn("Hai eliminato il listino CIPRESSO", html["html"])
        # And no progress bar: there's no recalculation to follow.
        self.assertEqual(html["barra"], "")

    def test_niente_barra_delle_fasi_per_uno_stato_in_attesa(self) -> None:
        avanzamento = self.esegui(
            "return renderAvanzamentoPipeline();",
            preparazione=self.con_confronto(pipeline=CAMBIAMENTO_ELIMINATO),
        )
        self.assertEqual(avanzamento, "")

    def test_la_fascia_arriva_anche_nelle_pagine_due_e_tre(self) -> None:
        html = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(pipeline=CAMBIAMENTO_ELIMINATO, passo=2),
        )
        self.assertIn("stale-comparison", html)
        # There "pages 2 and 3" means nothing: it talks about what's shown here.
        self.assertIn("I prezzi che vedi qui sotto sono ancora quelli di lunedì 10 agosto.", html)
        self.assertIn('data-action="vai-al-ricalcolo"', html)

    def test_un_cambiamento_malformato_non_rompe_la_pagina(self) -> None:
        """The field may be missing or malformed: falls back to the old behavior."""

        for pipeline in (
            {"stato": "IN_ATTESA"},
            {"stato": "IN_ATTESA", "cambiamento": None},
            {"stato": "IN_ATTESA", "cambiamento": "eliminato"},
            {"stato": "IN_ATTESA", "cambiamento": {"tipo": "boh", "documenti": ["x"]}},
        ):
            with self.subTest(pipeline=pipeline):
                cambiamento = self.esegui(
                    "return cambiamentoDocumenti();",
                    preparazione=self.con_confronto(pipeline=pipeline),
                )
                self.assertIsNone(cambiamento)

    def test_un_cambiamento_senza_nomi_si_dice_lo_stesso(self) -> None:
        frasi = self.esegui(
            "return frasiCambiamentoDocumenti();",
            preparazione=self.con_confronto(
                pipeline={"stato": "IN_ATTESA", "cambiamento": {"tipo": "eliminato", "documenti": [], "fornitori": []}},
            ),
        )
        self.assertEqual(frasi["titolo"], "Hai eliminato dei documenti dopo l’ultimo confronto.")


class UnSoloPulsantePrimario(BancoDiProva):
    """Page 1 says whose turn it is.

    Exercised tests.
    """

    def situazione(self, **kwargs) -> str:
        return self.esegui("return statoPaginaImporta();", preparazione=self.con_confronto(**kwargs))

    def comandi(self, **kwargs):
        return self.esegui(
            "return { confronto: comandoConfronto(), continua: comandoContinua() };",
            preparazione=self.con_confronto(**kwargs),
        )

    def test_la_tabella_degli_stati(self) -> None:
        casi = [
            ("FILE_SCELTI", {"pipeline": None, "pendenti": 1}),
            ("RICALCOLO_IN_CORSO", {"pipeline": {"stato": "IN_CORSO", "fasi": []}}),
            ("COLONNE_SCONOSCIUTE", {"pipeline": {"stato": "ERRORE", "fermata": {"code": "SCHEMA_SCONOSCIUTO"}}}),
            ("RICALCOLO_FALLITO", {"pipeline": {"stato": "ERRORE", "fermata": {"code": "ALTRO"}}}),
            ("DOCUMENTI_CAMBIATI", {"pipeline": CAMBIAMENTO_ELIMINATO}),
            ("CONFRONTO_AGGIORNATO", {"pipeline": {"stato": "COMPLETATO", "fasi": []}}),
        ]
        for atteso, kwargs in casi:
            with self.subTest(atteso=atteso):
                self.assertEqual(self.situazione(**kwargs), atteso)

    def test_il_ricalcolo_in_corso_batte_i_documenti_scelti(self) -> None:
        """With pending files AND the pipeline running, upload is disabled:
        offering "Upload chosen documents" as the primary action would be misleading."""

        self.assertEqual(
            self.situazione(pipeline={"stato": "IN_CORSO", "fasi": []}, pendenti=2),
            "RICALCOLO_IN_CORSO",
        )
        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}, pendenti=2),
        )
        carica = [pezzo.split(">")[0] for pezzo in html.split("<button") if 'data-action="upload"' in pezzo.split(">")[0]]
        self.assertTrue(carica)
        for pezzo in carica:
            self.assertIn("disabled", pezzo)

    def test_senza_documenti_non_si_puo_confrontare_e_si_dice_perche(self) -> None:
        vuota = dict(REVISIONE, files=[], products=[])
        comandi = self.comandi(pipeline=None, revisione=vuota)
        self.assertEqual(comandi["confronto"]["etichetta"], "Confronta i listini")
        self.assertTrue(comandi["confronto"]["disabilitato"])
        self.assertEqual(
            comandi["confronto"]["nota"],
            "Carica prima l’elenco dei prodotti e almeno un listino.",
        )

    def test_manca_solo_il_listino_e_lo_dice(self) -> None:
        solo_elenco = dict(REVISIONE, files=[REVISIONE["files"][0]], products=[])
        comandi = self.comandi(pipeline=None, revisione=solo_elenco)
        self.assertIn("Manca almeno un listino fornitore", comandi["confronto"]["nota"])

    def test_la_prima_volta_non_si_ri_calcola_niente(self) -> None:
        """Button label switches from "Recalculate" to "Compare price lists" when there is no comparison yet."""

        pronta = dict(REVISIONE, products=[])
        comandi = self.comandi(pipeline=None, revisione=pronta)
        self.assertEqual(comandi["confronto"]["etichetta"], "Confronta i listini")
        self.assertEqual(comandi["confronto"]["tono"], "primary")

    def test_dopo_un_confronto_il_verbo_cambia(self) -> None:
        comandi = self.comandi(pipeline={"stato": "COMPLETATO", "fasi": []})
        self.assertEqual(comandi["confronto"]["etichetta"], "Rifai il confronto")
        self.assertEqual(comandi["confronto"]["tono"], "secondary")
        self.assertEqual(comandi["continua"]["tono"], "primary")

    def test_documenti_cambiati_sposta_il_primario_sul_confronto(self) -> None:
        comandi = self.comandi(pipeline=CAMBIAMENTO_ELIMINATO)
        self.assertEqual(comandi["confronto"]["etichetta"], "Aggiorna il confronto")
        self.assertEqual(comandi["confronto"]["tono"], "primary")
        self.assertEqual(comandi["continua"]["tono"], "secondary")

    def test_continua_non_mente_mai(self) -> None:
        """With a stale comparison, doesn't promise today's prices."""

        for pipeline, atteso in (
            (CAMBIAMENTO_ELIMINATO, "Continua sul confronto di lunedì 10 agosto"),
            ({"stato": "IN_CORSO", "fasi": []}, "Vai al confronto di lunedì 10 agosto →"),
        ):
            with self.subTest(atteso=atteso):
                continua = self.comandi(pipeline=pipeline)["continua"]
                self.assertEqual(continua["etichetta"], atteso)
                self.assertFalse(continua["disabilitato"])

    def test_con_zero_prodotti_continua_dice_che_cosa_manca(self) -> None:
        vuota = dict(REVISIONE, products=[])
        continua = self.comandi(pipeline=None, revisione=vuota)["continua"]
        self.assertTrue(continua["disabilitato"])
        self.assertEqual(continua["nota"], "Prima carica i documenti e premi «Confronta i listini».")

    def test_ricalcolo_fallito_offre_di_riprovare(self) -> None:
        comandi = self.comandi(pipeline={"stato": "ERRORE", "fermata": {"code": "ALTRO"}})
        self.assertEqual(comandi["confronto"]["etichetta"], "Riprova il confronto")
        self.assertEqual(comandi["confronto"]["tono"], "primary")

    def test_in_ogni_stato_la_pagina_ha_al_piu_un_pulsante_primario(self) -> None:
        """The rule, checked against the page's real HTML."""

        casi = {
            "niente caricato": {"pipeline": None, "revisione": dict(REVISIONE, files=[], products=[])},
            "mai confrontato": {"pipeline": None, "revisione": dict(REVISIONE, products=[])},
            "confronto aggiornato": {"pipeline": {"stato": "COMPLETATO", "fasi": []}},
            "documenti cambiati": {"pipeline": CAMBIAMENTO_ELIMINATO},
            "ricalcolo in corso": {"pipeline": {"stato": "IN_CORSO", "fasi": []}},
            "ricalcolo fallito": {"pipeline": {"stato": "ERRORE", "fermata": {"code": "ALTRO"}}},
            "file scelti": {"pipeline": None, "pendenti": 1},
        }
        colonne = self.con_confronto(pipeline={"stato": "ERRORE", "fermata": {"code": "SCHEMA_SCONOSCIUTO"}}) + """
          state.schemaMapping.data = { documents: [{ profileId: "d1", fileName: "NUOVO.xlsx", format: "xlsx", sheets: [] }], suppliers: [] };
          state.schemaMapping.values = { d1: { role: "supplier", supplierChoice: "__new__", sheet: "", headerRow: 1, dataStartRow: 2, columns: {} } };
        """
        for nome, kwargs in list(casi.items()) + [("colonne sconosciute", None)]:
            with self.subTest(stato=nome):
                preparazione = colonne if kwargs is None else self.con_confronto(**kwargs)
                html = self.esegui("return renderUploadStep();", preparazione=preparazione)
                # Disabled primary buttons don't count: a disabled button
                # doesn't ask to be pressed.
                attivi = [
                    pezzo for pezzo in html.split("<button")
                    if "button--primary" in pezzo.split(">")[0] and "disabled" not in pezzo.split(">")[0]
                ]
                self.assertLessEqual(len(attivi), 1, f"{nome}: {[p.split('>')[0] for p in attivi]}")

    def test_nella_pagina_1_non_compare_gergo(self) -> None:
        """Not in the source: in the HTML the page actually produces."""

        casi = {
            "confronto aggiornato": {"pipeline": {"stato": "COMPLETATO", "fasi": []}},
            "documenti cambiati": {"pipeline": CAMBIAMENTO_ELIMINATO},
            "ricalcolo in corso": {"pipeline": {"stato": "IN_CORSO", "fasi": []}},
            "niente caricato": {"pipeline": None, "revisione": dict(REVISIONE, files=[], products=[])},
        }
        for nome, kwargs in casi.items():
            with self.subTest(stato=nome):
                html = self.esegui("return renderUploadStep();", preparazione=self.con_confronto(**kwargs)).lower()
                for gergo in ("payload", "pipeline", "adattatore", "impronta", "schema", "profilo", "master"):
                    self.assertNotIn(f">{gergo}", html, gergo)
                    self.assertNotIn(f" {gergo} ", html.replace("class=", "").replace("data-", ""), gergo)

    def test_durante_il_ricalcolo_i_comandi_sui_documenti_sono_spenti(self) -> None:
        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )
        elimina = [pezzo.split(">")[0] for pezzo in html.split("<button") if "ask-delete-upload" in pezzo.split(">")[0]]
        self.assertTrue(elimina, "il comando di eliminazione deve esserci, spento")
        for pezzo in elimina:
            self.assertIn("disabled", pezzo)
        # Anche la scelta dei file dal computer.
        for pezzo in html.split("<input"):
            if "data-picker-role" in pezzo.split(">")[0]:
                self.assertIn("disabled", pezzo.split(">")[0])
        self.assertIn("il confronto è in corso", html.lower())

    def test_le_colonne_si_confermano_senza_dover_provarle(self) -> None:
        """`conferma_schemi` reruns `valida_mappature` on its own: this check is optional."""

        preparazione = self.con_confronto(pipeline={"stato": "ERRORE", "fermata": {"code": "SCHEMA_SCONOSCIUTO"}}) + """
          state.schemaMapping.data = { documents: [{ profileId: "d1", fileName: "LISTINO NUOVO.xlsx", format: "xlsx", sheets: [] }], suppliers: [] };
          state.schemaMapping.values = { d1: { role: "supplier", supplierChoice: "__new__", sheet: "", headerRow: 1, dataStartRow: 2, columns: {} } };
        """
        html = self.esegui("return renderSchemaMappingWizard();", preparazione=preparazione)
        conferma = next(pezzo.split(">")[0] for pezzo in html.split("<button") if "confirm-schemas" in pezzo.split(">")[0])
        self.assertNotIn("disabled", conferma)
        self.assertIn("Conferma e riparti col confronto", html)
        self.assertIn("Prova le colonne", html)
        self.assertNotIn("Controlla le colonne", html)
        self.assertIn("Non riconosco le colonne di «LISTINO NUOVO.xlsx»", html)

    # -- "unknown" and "known but changed" are two different messages -------
    #
    # A user uploaded ONE new price list and saw the wizard open on the
    # CIPRESSO list too — recognized by the registry at 0.98 confidence, all
    # columns in place — with "Don't recognize the columns of 2 documents"
    # written above both.

    def con_documenti(self, *documenti: dict) -> str:
        import json as _json  # noqa: PLC0415

        valori = {str(voce["profileId"]): {
            "role": "supplier", "supplierChoice": "__new__", "sheet": "",
            "headerRow": 1, "dataStartRow": 2, "columns": {},
        } for voce in documenti}
        return self.con_confronto(pipeline={"stato": "ERRORE", "fermata": {"code": "SCHEMA_SCONOSCIUTO"}}) + f"""
          state.schemaMapping.data = {{ documents: {_json.dumps(list(documenti))}, suppliers: [] }};
          state.schemaMapping.values = {_json.dumps(valori)};
        """

    @staticmethod
    def documento(profilo: str, nome: str, stato: str, fornitore: str = "", cambiato: list | None = None) -> dict:
        return {
            "profileId": profilo, "fileName": nome, "format": "xlsx", "sheets": [],
            "reason": {"state": stato, "supplierName": fornitore, "changed": cambiato or []},
        }

    def test_un_listino_solo_cambiato_non_si_sente_dire_che_non_lo_conosco(self) -> None:
        html = self.esegui("return renderSchemaMappingWizard();", preparazione=self.con_documenti(
            self.documento("d1", "3listino_Cipresso.xlsx", "VARIATO", "CIPRESSO",
                           ["la riga delle intestazioni", "il nome del foglio"]),
        ))

        self.assertIn("Il listino «3listino_Cipresso.xlsx» è cambiato", html)
        self.assertNotIn("Non riconosco", html)
        self.assertIn("Questo listino è di CIPRESSO e lo conosco", html)
        self.assertIn("la riga delle intestazioni, il nome del foglio", html)

    def test_un_nuovo_e_un_cambiato_si_dicono_tutti_e_due(self) -> None:
        html = self.esegui("return renderSchemaMappingWizard();", preparazione=self.con_documenti(
            self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO"),
            self.documento("d2", "3listino_Cipresso.xlsx", "VARIATO", "CIPRESSO", ["il nome del foglio"]),
        ))

        self.assertIn("1 listino nuovo e 1 listino cambiato", html)
        # The explanation sits on the changed document's card, not the new one's.
        prima, dopo = html.split("3listino_Cipresso.xlsx", 1)
        self.assertNotIn("lo conosco", prima)
        self.assertIn("lo conosco", dopo)

    def test_senza_proposta_la_tendina_non_sceglie_per_te(self) -> None:
        """Preselecting something means confirming without looking still does something."""

        preparazione = self.con_documenti(self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO")) + """
          state.schemaMapping.data.suppliers = [{ id: "betulla", name: "BETULLA" }];
          state.schemaMapping.values.d1.supplierChoice = "";
        """
        html = self.esegui("return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
                           preparazione=preparazione)

        tendina = html.split('data-schema-field="supplierChoice"')[1].split("</select>")[0]
        self.assertIn('<option value="" selected>Scegli il fornitore…</option>', tendina)
        self.assertNotIn('value="betulla" selected', tendina)
        self.assertNotIn('value="__new__" selected', tendina)

    def test_assegnare_a_un_fornitore_che_ha_gia_un_listino_lo_dice_prima(self) -> None:
        """Assigning to a supplier that already has a price list silently drops the older file."""

        preparazione = self.con_documenti(self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO")) + """
          state.schemaMapping.data.suppliers = [{ id: "betulla", name: "BETULLA" }];
          state.schemaMapping.data.occupied = [{
            role: "supplier", supplierId: "betulla", supplierName: "BETULLA",
            fileName: "LISTINO BETULLA.xlsx", modifiedAt: "2026-08-18T10:00:00",
          }];
          state.schemaMapping.data.documents[0].modifiedAt = "2026-08-21T10:00:00";
          state.schemaMapping.values.d1.supplierChoice = "betulla";
        """
        html = self.esegui("return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
                           preparazione=preparazione)

        self.assertIn("C’è già un listino di questo fornitore", html)
        self.assertIn("resterebbe fuori «LISTINO BETULLA.xlsx»", html)

    def test_su_un_fornitore_nuovo_non_si_dice_niente(self) -> None:
        preparazione = self.con_documenti(self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO")) + """
          state.schemaMapping.data.suppliers = [{ id: "betulla", name: "BETULLA" }];
          state.schemaMapping.data.occupied = [{
            role: "supplier", supplierId: "betulla", supplierName: "BETULLA",
            fileName: "LISTINO BETULLA.xlsx", modifiedAt: "2026-08-18T10:00:00",
          }];
          state.schemaMapping.values.d1.supplierChoice = "__new__";
        """
        html = self.esegui("return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
                           preparazione=preparazione)

        self.assertNotIn("C’è già un listino", html)

    def test_un_errore_delle_colonne_non_porta_via_le_tendine(self) -> None:
        """A column error must not replace the whole form, leaving nothing to fix."""

        preparazione = self.con_documenti(self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO")) + """
          state.schemaMapping.error = "OFFERTE.xlsx: completa prezzo, pezzi per collo.";
        """
        html = self.esegui("return renderSchemaMappingWizard();", preparazione=preparazione)

        self.assertIn("completa prezzo, pezzi per collo", html)
        self.assertIn("data-schema-document", html)
        self.assertIn("confirm-schemas", html)


class TestiInItaliano(BancoDiProva):
    """The rewritten copy, checked against the HTML the page actually produces.

    Exercised tests.
    """

    def test_eliminare_un_listino_dice_che_cosa_succede_davvero(self) -> None:
        preparazione = self.con_confronto(pipeline=None) + """
          state.uploadDeletion.confirming = "LISTINO CIPRESSO.xlsx";
        """
        html = self.esegui("return renderUploadStep();", preparazione=preparazione)
        self.assertIn("Tolgo «LISTINO CIPRESSO.xlsx» dal programma?", html)
        self.assertIn("Il file sul tuo computer resta dov’è.", html)
        self.assertIn("I prezzi in pagina restano quelli finché non rifai il confronto.", html)
        self.assertNotIn("Eliminare questo listino?", html)

    def storico(self, *, confermando: bool) -> str:
        preparazione = self.con_confronto(pipeline=None, passo=3) + f"""
          state.compilazioni.caricate = true;
          state.compilazioni.elenco = [{{ cartella: "2026-08-10", etichetta: "10 agosto 2026", file: [] }}];
          state.compilazioni.confermaElimina = {'"2026-08-10"' if confermando else '""'};
        """
        return self.esegui("return renderCompilazioniPrecedenti();", preparazione=preparazione)

    def test_il_comando_di_eliminazione_nomina_gli_ordini(self) -> None:
        html = self.storico(confermando=False)
        self.assertIn("Elimina questi ordini", html)
        self.assertNotIn("Elimina compilazione", html)

    def test_eliminare_una_compilazione_nomina_la_data_e_i_promemoria(self) -> None:
        html = self.storico(confermando=True)
        self.assertIn("Elimino gli ordini del 10 agosto 2026?", html)
        self.assertIn("è arrivata la merce?", html)
        self.assertIn("Non si può annullare.", html)
        self.assertNotIn("Eliminare questa compilazione e i suoi promemoria?", html)

    def test_il_minimo_d_ordine_si_chiama_sempre_cosi(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=3)
        html = self.esegui("return renderCompileStep();", preparazione=preparazione)
        # The product totals 30 € against a 500 minimum: the warning must show.
        self.assertIn("al minimo d’ordine di CIPRESSO", html)
        for gergo in ("soglia netta", "minimo netto", "sotto soglia"):
            self.assertNotIn(gergo, html)

    def test_la_conferma_dice_subito_perche_serve(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["requiresConfirmation"] = True
        preparazione = self.con_confronto(pipeline=None, passo=2, revisione=revisione)
        html = self.esegui("return renderConfirmation(state.review.products[0]);", preparazione=preparazione)
        # A question, and it reads as one: the two answers are two matching
        # buttons, not a checkbox and a button.
        self.assertIn("È lo stesso articolo?", html)
        self.assertIn("Sì, è lo stesso", html)
        self.assertIn("No, non è lo stesso", html)
        self.assertIn("Il codice a barre non coincide: confronta nome e formato con quello qui sotto.", html)
        # The reason is shown directly, not behind a collapsed panel.
        self.assertNotIn("Perché serve?", html)
        self.assertNotIn("Conferma richiesta", html)

    def test_gli_scarti_si_chiamano_come_sono(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["auditSummary"] = {
            "inputs": [{
                "supplier_id": "cipresso",
                "role": "supplier",
                "records": 100,
                "rows_not_orderable": {"senza prezzo": 4},
                "reading": {"rows_kept": 100},
            }],
        }
        html = self.esegui(
            "return renderDiscardedRowsPanel();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione),
        )
        self.assertIn("Righe di listino rimaste fuori", html)
        self.assertIn("Quali righe non sono entrate nel confronto e perché.", html)
        self.assertNotIn("Scarti e segnalazioni", html)
        self.assertNotIn("Dettagli tecnici", html)

    def test_i_motivi_dello_scarto_si_leggono_in_italiano(self) -> None:
        """The service sends machine labels; the page doesn't just copy them.

        This panel receives raw codes like `senza_prezzo` and
        `DISPLAY_COMPONENT` — a variable name, not an explanation — and must
        translate them.
        """

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["auditSummary"] = {
            "inputs": [{
                "supplier_id": "cipresso",
                "role": "supplier",
                "records": 100,
                "rows_not_orderable": {"senza_prezzo": 4, "DISPLAY_COMPONENT": 2},
                "reading": {"rows_kept": 100, "rows_excluded": {"riga_fuori_dal_filtro": 3}},
            }],
        }
        html = self.esegui(
            "return renderDiscardedRowsPanel();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione),
        )

        self.assertIn("4 senza prezzo", html)
        self.assertIn("2 componenti di un espositore", html)
        self.assertIn("3 fuori dal filtro del listino", html)
        self.assertNotIn("senza_prezzo", html)
        self.assertNotIn("DISPLAY_COMPONENT", html)
        self.assertNotIn("riga_fuori_dal_filtro", html)
        # 4 + 2 not orderable, 3 excluded: the total reads at the top.
        self.assertIn("9 righe scartate", html)

    def test_un_motivo_che_la_pagina_non_conosce_si_mostra_com_e(self) -> None:
        """Discard labels are written by whoever configures the price list.

        Inventing a translation would hide the real label: a raw technical
        word beats nothing.
        """

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["auditSummary"] = {
            "inputs": [{
                "supplier_id": "cipresso",
                "role": "supplier",
                "records": 100,
                "rows_not_orderable": {},
                "reading": {"rows_kept": 100, "rows_excluded": {"reparto surgelati": 7}},
            }],
        }
        html = self.esegui(
            "return renderDiscardedRowsPanel();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione),
        )

        self.assertIn("7 reparto surgelati", html)

    def test_il_titolo_del_messaggio_di_caricamento(self) -> None:
        preparazione = self.con_confronto(pipeline=None) + 'state.uploadMessage = "2 documenti caricati.";'
        html = self.esegui("return renderUploadStep();", preparazione=preparazione)
        self.assertIn("Documenti caricati", html)

    def test_annullato_non_arriva(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=2) + """
          state.history.pending = normalizePendingOrders([
            { orderId: "o1", supplier: "cipresso", supplierName: "CIPRESSO", createdAt: "2026-08-03T10:00:00+02:00", lineCount: 3, totalNet: 400 },
          ]);
        """
        html = self.esegui("return renderPendingOrdersPanel();", preparazione=preparazione)
        self.assertIn("Annullato, non arriva", html)
        self.assertIn("Chi annulli sparisce dall’elenco e non ti viene più chiesto.", html)
        self.assertNotIn("NON ARRIVERÀ PIÙ", html)

    def test_la_compilazione_non_riuscita_promette_solo_quello_che_puo(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileFailure = { message: "I listini non sono stati preparati. Le tue scelte sono salvate: puoi riprovare col pulsante qui sotto.", detail: "KeyError: 'supplier'" };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertIn("Le tue scelte sono salvate: puoi riprovare col pulsante qui sotto.", html)
        # The technical detail doesn't disappear: it moves into a collapsed panel.
        self.assertIn("KeyError", html)
        self.assertIn("<details", html)
        self.assertNotIn("Compilazione non riuscita:", html)

    def test_il_badge_in_alto_non_dice_pronto_a_chi_non_ha_niente(self) -> None:
        vuota = dict(REVISIONE, files=[], products=[])
        self.assertFalse(self.esegui(
            "return confrontoDisponibile();",
            preparazione=self.con_confronto(pipeline=None, revisione=vuota),
        ))
        # E con un confronto vero torna disponibile.
        pieno = self.esegui("return confrontoDisponibile();", preparazione=self.con_confronto(pipeline=None))
        self.assertTrue(pieno)


class QuattroRilievi(BancoDiProva):
    """Four separate fixes, each covered by its own tests."""

    def test_la_decisione_su_una_riga_proposta_si_vede_e_si_cambia(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["offers"].append({
            "supplierId": "betulla",
            "price": 0,
            "available": False,
            "candidateDecision": "rejected",
            "rejectedCandidate": {
                "candidateKey": "k1",
                "supplierId": "betulla",
                "supplierName": "BETULLA",
                "description": "Prodotto uno formato famiglia",
                "orderUnitPriceNet": 9,
                "quantityFactor": 6,
                "available": True,
            },
        })
        html = self.esegui(
            "return renderCandidateDecisions(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, passo=2, revisione=revisione),
        )
        self.assertIn("NON è lo stesso articolo", html)
        self.assertIn("Prodotto uno formato famiglia", html)
        # And it's reversible: same command as the question, opposite answer.
        self.assertIn('data-action="answer-candidate"', html)
        self.assertIn('data-accepted="true"', html)
        self.assertIn('data-candidate-key="k1"', html)

    def test_una_decisione_accettata_si_puo_disfare(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["offers"][0].update({
            "candidateDecision": "accepted",
            "rejectedCandidate": {"candidateKey": "k2", "description": "Riga proposta", "available": True},
        })
        html = self.esegui(
            "return renderCandidateDecisions(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, passo=2, revisione=revisione),
        )
        self.assertIn("è lo stesso articolo", html)
        self.assertIn('data-accepted="false"', html)

    def test_senza_decisioni_non_si_disegna_niente(self) -> None:
        html = self.esegui(
            "return renderCandidateDecisions(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, passo=2),
        )
        self.assertEqual(html, "")

    def test_la_riassegnazione_silenziosa_viene_contata_e_dichiarata(self) -> None:
        """The saved supplier has no offer left: the page reassigns it, and discloses the move."""

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"] = [
            {
                "id": f"p{indice}",
                "name": f"Prodotto {indice}",
                "quantity": 1,
                "selectedSupplierId": "cipresso",
                "offers": [
                    {"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": False},
                    {"supplierId": "betulla", "price": 9, "unitsPerOrderUnit": 6, "available": True},
                ],
            }
            for indice in range(1, 4)
        ]
        esito = self.esegui(
            "return { quante: state.review.supplierReassignments.length, html: renderSupplierReassignments() };",
            preparazione=self.con_confronto(pipeline=None, passo=2, revisione=revisione),
        )
        self.assertEqual(esito["quante"], 3)
        self.assertIn("3 prodotti erano assegnati a CIPRESSO: ora sono passati a BETULLA.", esito["html"])

    def test_un_prodotto_mai_assegnato_non_conta_come_spostato(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["selectedSupplierId"] = ""
        esito = self.esegui(
            "return state.review.supplierReassignments;",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione),
        )
        self.assertEqual(esito, [])

    def test_writer_issues_e_history_issues_arrivano_in_pagina(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileResult = {
            ok: true,
            message: "3 listini pronti.",
            zipUrl: "/scarica/ordini.zip",
            zipNome: "ordini.zip",
            outputs: [],
            writerIssues: ["La copia di LARICE non è stata verificata e non è stata tenuta."],
            historyIssues: ["Questo ordine non entra fra quelli da controllare la prossima settimana."],
          };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertIn("La copia di LARICE non è stata verificata", html)
        self.assertIn("non entra fra quelli da controllare", html)
        # And it doesn't present as fully successful.
        self.assertIn("results--partial", html)
        self.assertIn("non tutto è andato a buon fine", html)

    def test_una_compilazione_riuscita_e_pulita_non_si_presenta_come_a_meta(self) -> None:
        """A clean, successful run doesn't present as partial.

        The local service never sends a per-supplier error on a successful
        run — either everything compiles, or `run_writer` stops and the page
        shows `renderCompileFailure` instead. This test guards the real
        invariant: without warnings, the panel is the full one, not the
        "partial" one. The service contract is pinned by
        `test_web_app.LaCompilazioneRiuscitaNonHaErroriPerFornitoreTests`.
        """

        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileResult = {
            ok: true, message: "2 listini pronti.", outputs: [],
            zipUrl: "/ordini/x/zip", zipNome: "ordini.zip",
          };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertIn("2 listini pronti.", html)
        self.assertNotIn("results--partial", html)
        self.assertNotIn("non tutto è andato a buon fine", html)

    def test_una_compilazione_senza_audit_lo_dice_invece_di_inventare_un_totale(self) -> None:
        """A safeguard with no dedicated regression test of its own.

        The folder exists and the documents can be downloaded, but if the
        audit can't be read, supplier, total and row count can't be shown —
        and the page must say so instead of displaying a total nobody computed.
        """

        html = self.esegui(
            """
            return renderCompilazione({
              cartella: "2026-08-14_1430",
              etichetta: "14 agosto 2026, 14:30",
              completa: false,
              stato: "SCONOSCIUTO",
              fornitori: [],
              totaleNetto: null,
              righe: null,
              listini: 1,
              file: [{ nome: "Ordine LARICE — 14 agosto 2026.xlsx", tipo: "listino", url: "/ordini/x/y" }],
              mancanti: [],
              avvisi: [],
              zipUrl: "/ordini/2026-08-14_1430/zip",
              zipNome: "ordini.zip",
            });
            """,
            preparazione=self.con_confronto(pipeline=None, passo=3),
        )
        self.assertIn("Dettagli non leggibili", html)
        self.assertIn("I dettagli di questa compilazione non si leggono", html)
        self.assertIn("is-incompleta", html)
        # The documents remain downloadable: it's the audit that's missing, not the files.
        self.assertIn("/ordini/2026-08-14_1430/zip", html)

    def test_un_documento_spostato_dalla_sua_cartella_si_dice(self) -> None:
        html = self.esegui(
            """
            return renderCompilazione({
              cartella: "2026-08-14_1430",
              etichetta: "14 agosto 2026, 14:30",
              completa: true,
              stato: "FILES_READY",
              fornitori: [],
              totaleNetto: 120.5,
              righe: 3,
              listini: 0,
              file: [],
              mancanti: ["Ordine LARICE — 14 agosto 2026.xlsx"],
              avvisi: [],
              zipUrl: null,
              zipNome: "ordini.zip",
            });
            """,
            preparazione=self.con_confronto(pipeline=None, passo=3),
        )
        self.assertIn("Un documento di questa compilazione non è più nella sua cartella", html)
        self.assertIn("Ordine LARICE", html)
        self.assertIn("Se li hai spostati tu va bene", html)

    def test_gli_avvisi_della_consegna_colorano_il_riquadro(self) -> None:
        """The document exists and is correct, but couldn't be given a readable name.

        This detail must surface as its own signal, not only inside the plain
        `message` text — otherwise the panel would stay green with the
        primary button, looking like a perfect delivery.
        """

        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileResult = {
            ok: true,
            message: "1 copia del listino generata.",
            zipUrl: "/scarica/ordini.zip",
            zipNome: "ordini.zip",
            outputs: [],
            writerIssues: [],
            deliveryIssues: ["Il documento di LARICE è rimasto con questo nome: Il file è in uso."],
            historyIssues: [],
          };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertIn("è rimasto con questo nome", html)
        self.assertIn("Documenti consegnati, ma con qualcosa da sapere", html)
        self.assertIn("results--partial", html)
        # And the document stays downloadable: the warning isn't a rejection.
        self.assertIn("/scarica/ordini.zip", html)

    def test_una_compilazione_pulita_resta_verde(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileResult = { ok: true, message: "3 listini pronti.", outputs: [], writerIssues: [], historyIssues: [] };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertNotIn("results--partial", html)
        self.assertIn("3 listini pronti.", html)

    def test_l_annulla_dell_esclusione_non_sopravvive_a_un_confronto_nuovo(self) -> None:
        """Was restoring supplier and confirmation from the PREVIOUS comparison onto a new product."""

        esito = self.esegui(
            """
              state.exclusionUndo = { id: "p1", name: "Prodotto uno", quantity: 9, selectedSupplierId: "betulla", confirmed: true };
              state.review = normalizeReview(%s);
              restoreExcludedProducts();
              return { undo: state.exclusionUndo, html: renderQuantityStep() };
            """ % json.dumps(REVISIONE),
            preparazione=self.con_confronto(pipeline=None, passo=2),
        )
        self.assertIsNone(esito["undo"])
        self.assertNotIn("undo-exclude", esito["html"])


class AsserzioniSulSorgente(unittest.TestCase):
    """What isn't exercised: checked against the source, and declared as such.

    These are NOT exercised tests. They look at the text of `app.js` and
    `styles.css`. They cover two things the harness can't see: click handlers
    (registered via a fake `addEventListener`) and whether new CSS classes have
    a rule at all. A stylesheet that declares the class but styles it wrong
    still passes: there's no browser here.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.js = APP_JS.read_text(encoding="utf-8")
        cls.css = STYLES.read_text(encoding="utf-8")

    def corpo(self, nome: str) -> str:
        marcatore = f"function {nome}("
        self.assertIn(marcatore, self.js)
        return self.js.split(marcatore, 1)[1].split("\n}\n", 1)[0]

    def test_il_comando_della_fascia_e_collegato(self) -> None:
        self.assertIn('if (action === "vai-al-ricalcolo") goToStep(1);', self.js)

    def test_l_avvio_distingue_i_due_significati_di_in_attesa(self) -> None:
        self.assertIn(
            'if (!esito || (esito === "IN_ATTESA" && !cambiamentoDocumenti(stato))) return;',
            self.js,
        )
        # And the old line, which conflated the two, is gone.
        self.assertNotIn('if (!esito || esito === "IN_ATTESA") return;', self.js)

    def test_il_trascinamento_rispetta_il_ricalcolo_in_corso(self) -> None:
        self.assertIn("if (pipelineInCorso()) {", self.corpo("addPendingFiles"))

    def test_le_classi_nuove_hanno_uno_stile(self) -> None:
        for classe in (
            "stale-comparison", "stale-comparison__icon", "stale-comparison__body",
            "button-hint", "import-locked", "candidate-decision", "candidate-decisions",
            "reassignment-list", "results--partial", "results--failed", "results__issues",
            "confirmation__why", "pending-order__note", "schema-mapping__nota",
            "pending-orders--failed",
            "offer-grid__head", "offer-card__label", "offer-card__numero",
            "offer-card__intera", "offer-grid__mancanti", "alert--gruppo",
            "alert__elenco", "dropzone--riga", "button--small",
            "supplier-totals-strip__item--totale",
            "confirmation__gia", "doc-columns__note--manca",
            "order-lines__pezzo", "order-lines__pezzo-etichetta", "order-lines__intestazione",
        ):
            with self.subTest(classe=classe):
                self.assertIn(f".{classe}", self.css)

    def test_le_colonne_dei_numeri_non_sono_larghe_quanto_il_contenuto(self) -> None:
        """The two summary columns whose widths must stay aligned across rows.

        Substring check: there's no browser here, so this looks at the rule,
        not the rendered effect. That the columns really line up was measured
        in a real browser (rows priced "9.06 €" and "17.85 €" had their last
        column at different widths, dragging the − 1 + × controls out of line
        with the rest of the row).
        """

        regola = self.css.split(".order-lines li {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: minmax(0, 1fr) 5.5rem 8.75rem 5.5rem 5.5rem;", regola)
        self.assertNotIn("auto", regola.split("grid-template-columns:", 1)[1].split(";", 1)[0])
        # Same for the per-product view: the header only aligns if no column
        # after the name depends on its content's width.
        per_prodotto = self.css.split(".order-lines--product li {", 1)[1].split("}", 1)[0]
        self.assertIn("minmax(8rem, 0.45fr) 5.5rem 8.75rem 5.5rem 5.5rem;", per_prodotto)

    def test_sotto_i_1050_px_il_prezzo_al_pezzo_resta_accanto_al_totale(self) -> None:
        """Measured in a real browser, per-product view at 900 px: with quantity
        on its own row, the controls row put the unit price under the supplier
        name and the total mid-row. Below 760 px the phone rule wins (its own
        row, under the total), since it comes later with equal specificity."""

        media_1050 = self.css.split("@media (max-width: 1050px)", 1)[1].split("@media", 1)[0]
        self.assertIn(".order-lines--product .order-lines__pezzo {\n    grid-column: 4;", media_1050)
        self.assertIn(".order-lines--product li > strong {\n    grid-column: 5;", media_1050)
        media_760 = self.css.split("@media (max-width: 760px)", 1)[1].split("@media", 1)[0]
        self.assertIn(".order-lines .order-lines__pezzo {\n    order: 1;\n    grid-column: 1 / -1;", media_760)


class UnaRichiestaFallitaNonSparisce(BancoDiProva):
    """A failed request must not look the same as "nothing happened".

    Three places where the page could conflate them: pending orders, the
    recalculation progress poll, and a response that claims to be JSON and
    isn't.
    """

    def test_gli_ordini_in_sospeso_non_letti_si_dichiarano(self) -> None:
        """Distinguishes "nothing pending" from "couldn't find out": not the same message."""

        valore = self.esegui(
            "return loadPendingOrders().then(() => ({"
            " errore: state.history.errore,"
            " riquadro: renderPendingOrdersPanel(),"
            "}));",
            preparazione=self.con_confronto(pipeline=None, passo=2),
            # No response configured for /api/history/pending: the request fails.
            risposte={},
        )

        self.assertIn("Non sono riuscito a leggere gli ordini già fatti", valore["errore"])
        self.assertIn("Ordini non ancora ricevuti", valore["riquadro"])
        self.assertIn("pending-orders--failed", valore["riquadro"])
        self.assertIn("Controlla tu quali ordini sono già partiti", valore["riquadro"])

    def test_un_elenco_davvero_vuoto_non_disegna_niente(self) -> None:
        valore = self.esegui(
            "return loadPendingOrders().then(() => ({"
            " errore: state.history.errore,"
            " riquadro: renderPendingOrdersPanel(),"
            "}));",
            preparazione=self.con_confronto(pipeline=None, passo=2),
            risposte={"/api/history/pending": {"pending": []}},
        )

        self.assertEqual(valore["errore"], "")
        self.assertEqual(valore["riquadro"], "")

    def test_una_risposta_che_dice_json_e_non_lo_e_non_e_un_corpo_vuoto(self) -> None:
        """With `{}` in place of the error, /api/review must not report "0 products"."""

        valore = self.esegui(
            "return requestJson('/api/review').then("
            "  () => 'NESSUN ERRORE',"
            "  (errore) => errore.message,"
            ");",
            risposte={"/api/review": {"__jsonRotto": True}},
        )

        self.assertNotEqual(valore, "NESSUN ERRORE")
        self.assertIn("non si riesce a leggere", valore)

    def test_il_confronto_non_diventa_vuoto_per_una_risposta_illeggibile(self) -> None:
        """The consequence that matters: the page stays on an error, not on zero products."""

        valore = self.esegui(
            "return loadReview().then(() => ({"
            " prodotti: state.review ? state.review.products.length : null,"
            " errore: state.runtimeError,"
            "}));",
            risposte={"/api/review": {"__jsonRotto": True}},
        )

        self.assertIsNone(valore["prodotti"], "un confronto illeggibile non è un confronto vuoto")
        self.assertIn("non si riesce a leggere", valore["errore"])

    def test_l_avanzamento_fermo_lo_dice_dopo_qualche_tentativo(self) -> None:
        valore = self.esegui(
            "state.pipeline.stato = { stato: 'IN_CORSO', avanzamento: { percento: 40 }, fasi: [] };"
            "state.pipeline.chiesto = true;"
            "const passi = [];"
            "return (async () => {"
            "  for (let giro = 0; giro < 5; giro += 1) {"
            "    await controllaPipeline();"
            "    passi.push(state.pipeline.contattoPerso ? 'detto' : 'zitto');"
            "  }"
            "  return { passi, barra: renderAvanzamentoPipeline() };"
            "})();",
            preparazione=self.con_confronto(pipeline=None),
            risposte={},
        )

        # The first few missed polls aren't worth reporting; the fifth is.
        self.assertEqual(valore["passi"], ["zitto", "zitto", "zitto", "zitto", "detto"])
        self.assertIn("Non so più a che punto è", valore["barra"])
        self.assertIn("Il confronto continua per conto suo", valore["barra"])

    def test_un_controllo_riuscito_cancella_l_avviso(self) -> None:
        """When the service starts answering again, the warning clears: it doesn't stick around."""

        valore = self.esegui(
            "state.pipeline.chiesto = true;"
            "state.pipeline.controlliPersi = 9;"
            "state.pipeline.contattoPerso = 'vecchio avviso';"
            "return controllaPipeline().then(() => ({"
            " contatto: state.pipeline.contattoPerso,"
            " persi: state.pipeline.controlliPersi,"
            "}));",
            preparazione=self.con_confronto(pipeline=None),
            risposte={"/api/pipeline/stato": {"stato": "IN_CORSO", "avanzamento": {"percento": 60}, "fasi": []}},
        )

        self.assertEqual(valore["contatto"], "")
        self.assertEqual(valore["persi"], 0)


class IProdottiFermiSiLeggonoTuttiTests(BancoDiProva):
    """With a dozen blocked products, only three names showed, plus "and 9 more".

    The identifiers were already in the service response — one per failed
    check — and the page was discarding them: no way to find the rows to fix
    among hundreds.
    """

    def test_il_corpo_della_risposta_rifiutata_arriva_alla_pagina(self) -> None:
        valore = self.esegui(
            "return requestJson('/api/compile', { method: 'POST' }).then("
            "  () => null,"
            "  (errore) => (errore.dettagli && errore.dettagli.errors) || null,"
            ");",
            risposte={"/api/compile": {
                "__stato": 422,
                "message": "Controlli non superati.",
                "errors": [
                    {"code": "CONFERMA_MANCANTE", "productId": "p1", "productName": "PASTA"},
                    {"code": "OFFERTA_NON_VALIDA", "productId": "p2", "productName": "OLIO", "supplierName": "BETULLA"},
                ],
            }},
        )

        self.assertEqual([voce["productName"] for voce in valore], ["PASTA", "OLIO"])

    def test_i_nomi_dei_prodotti_fermi_si_vedono_uno_per_riga(self) -> None:
        html = self.esegui(
            "state.compileFailure = {"
            "  message: 'I listini non sono stati preparati.',"
            "  detail: 'Controlli non superati.',"
            "  controlli: [{ productId: 'p1', productName: 'PASTA MEZZE MANICHE', supplierName: '' },"
            "              { productId: 'p2', productName: 'OLIO EXTRAVERGINE', supplierName: 'BETULLA' }],"
            "};"
            "return renderCompileFailure();",
            preparazione=self.con_confronto(pipeline=None, passo=3),
        )

        self.assertIn("2 prodotti sono fermi", html)
        self.assertIn("PASTA MEZZE MANICHE", html)
        self.assertIn("OLIO EXTRAVERGINE", html)
        self.assertIn("BETULLA", html)

    def test_senza_controlli_il_riquadro_resta_quello_di_prima(self) -> None:
        html = self.esegui(
            "state.compileFailure = { message: 'Non preparati.', detail: 'Disco pieno.' };"
            "return renderCompileFailure();",
            preparazione=self.con_confronto(pipeline=None, passo=3),
        )

        self.assertIn("Non preparati.", html)
        self.assertNotIn("prodotti sono fermi", html)


class VersioneDelloStatoFraDueSchede(BancoDiProva):
    """The page declares which state version it started from.

    Without this number the local service can't distinguish two tabs open on
    the same comparison — same run — and the second tab's autosave overwrites
    the first tab's work.
    """

    def test_la_pagina_dichiara_al_servizio_la_versione_da_cui_e_partita(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["state"] = {"currentStep": 1, "stateVersion": 5}

        valore = self.esegui(
            "return loadReview().then(() => ({ letta: state.stateVersion, mandata: snapshot().stateVersion }));",
            risposte={"/api/review": revisione},
        )

        self.assertEqual(valore["letta"], 5)
        self.assertEqual(valore["mandata"], 5)

    def test_senza_il_dato_la_versione_resta_zero(self) -> None:
        """An older service response must not cause `undefined` to be sent."""

        valore = self.esegui(
            "return loadReview().then(() => snapshot().stateVersion);",
            risposte={"/api/review": REVISIONE},
        )

        self.assertEqual(valore, 0)

    def test_il_salvataggio_raccoglie_la_versione_nuova(self) -> None:
        """Whoever just saved must not have their next save rejected."""

        valore = self.esegui(
            "state.dirty = true;"
            "return saveState().then((riuscito) => ({ riuscito, versione: state.stateVersion }));",
            preparazione=self.con_confronto(pipeline=None),
            risposte={"/api/state": {"ok": True, "savedAt": "2026-08-14T10:00:00Z", "stateVersion": 3}},
        )

        self.assertTrue(valore["riuscito"])
        self.assertEqual(valore["versione"], 3)

    def test_una_risposta_senza_versione_non_azzera_quella_che_c_era(self) -> None:
        valore = self.esegui(
            "state.stateVersion = 4; state.dirty = true;"
            "return saveState().then(() => state.stateVersion);",
            preparazione=self.con_confronto(pipeline=None),
            risposte={"/api/state": {"ok": True, "savedAt": "2026-08-14T10:00:00Z"}},
        )

        self.assertEqual(valore, 4)


class OrdineDellElenco(BancoDiProva):
    """Product ordering on page 2 — exercised tests.

    Products in the comparison need to be sortable alphabetically. With
    hundreds of rows, the management-software order works fine for whoever
    has the paper document in hand, and is useless for anyone searching for a
    specific product.
    """

    SCAFFALE = {
        **REVISIONE,
        "products": [
            {"id": "p1", "name": "ZUCCHERO DI CANNA", "ean": "1",
             "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
            {"id": "p2", "name": "NEVAL CREMA 100ML", "ean": "2",
             "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
            {"id": "p3", "name": "aceto di mele", "ean": "3",
             "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
            {"id": "p4", "name": "NEVAL CREMA 10ML", "ean": "4",
             "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
            {"id": "p5", "name": "Èlite salviette", "ean": "5",
             "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
        ],
    }

    def nomi(self, ordine: str | None) -> list[str]:
        scelta = f"state.filters.sort = {json.dumps(ordine)};" if ordine else ""
        return self.esegui(
            f"{scelta} return filteredProducts().map((prodotto) => prodotto.name);",
            preparazione=self.con_confronto(revisione=self.SCAFFALE, passo=2),
        )

    def test_il_predefinito_resta_l_ordine_del_gestionale(self) -> None:
        """The order the person placing the order wrote the products in: not lost."""

        self.assertEqual(self.nomi(None), [
            "ZUCCHERO DI CANNA", "NEVAL CREMA 100ML", "aceto di mele",
            "NEVAL CREMA 10ML", "Èlite salviette",
        ])

    def test_dalla_a_alla_z_conta_i_numeri_come_numeri(self) -> None:
        """Numeric-aware sort: "10ML" sorts before "100ML"; case and accents fall in their natural place."""

        self.assertEqual(self.nomi("nome"), [
            "aceto di mele", "Èlite salviette", "NEVAL CREMA 10ML",
            "NEVAL CREMA 100ML", "ZUCCHERO DI CANNA",
        ])

    def test_dalla_z_alla_a_e_l_ordine_rovesciato(self) -> None:
        self.assertEqual(self.nomi("nome-desc"), list(reversed([
            "aceto di mele", "Èlite salviette", "NEVAL CREMA 10ML",
            "NEVAL CREMA 100ML", "ZUCCHERO DI CANNA",
        ])))

    def test_ordinare_non_tocca_l_elenco_del_confronto(self) -> None:
        """`sort` mutates in place: without copying it would reorder `state.review`.

        Calls the function with the real list, as any caller would — going
        through `filteredProducts()` wouldn't catch a missing copy, since
        `filter` already returns a new array.
        """

        prima = self.esegui(
            "state.filters.sort = 'nome'; sortedProducts(state.review.products);"
            "return state.review.products.map((prodotto) => prodotto.name);",
            preparazione=self.con_confronto(revisione=self.SCAFFALE, passo=2),
        )
        self.assertEqual(prima[0], "ZUCCHERO DI CANNA")

    def test_l_ordinamento_non_spegne_il_filtro_delle_offerte(self) -> None:
        """Changing the sort order isn't a filter, and must not clear one."""

        sorgente = APP_JS.read_text(encoding="utf-8")
        blocco = sorgente.split('if (target.dataset.filter && target.dataset.filter !== "search")')[1][:600]
        self.assertIn('if (target.dataset.filter !== "sort") state.filters.promotionId = null;', blocco)

    def test_la_scelta_e_in_pagina(self) -> None:
        html = self.esegui(
            "return renderToolbar();",
            preparazione=self.con_confronto(revisione=self.SCAFFALE, passo=2),
        )
        self.assertIn('data-filter="sort"', html)
        self.assertIn("Nome (dalla A alla Z)", html)
        self.assertIn("Nome (dalla Z alla A)", html)
        self.assertIn("Ordine del gestionale", html)


class SezioniCheRestanoAperte(BancoDiProva):
    """Collapsible panels don't close themselves on rerender — exercised tests.

    While the pipeline runs, the page rerenders every second, and `render()`
    doesn't patch the content, it replaces it (`innerHTML = …`). A
    `<details>` element's open state lives on the node, not in the markup
    string that produced it — a naive rerender loses it.

    What this class does NOT cover: the harness has no real DOM, so it
    doesn't test that a user gesture reaches the listener. It tests the
    function the listener calls, and that rendering respects the remembered
    state across a second render.
    """

    def test_la_barra_del_ricalcolo_riparte_aperta_se_era_aperta(self) -> None:
        html = self.esegui(
            'state.aperti["ricalcolo"] = true; return renderAvanzamentoPipeline();',
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertIn('class="pipeline-details" data-ricorda="ricalcolo" open', html)

    def test_se_non_l_hai_aperta_resta_chiusa(self) -> None:
        """`open` isn't hardcoded in the markup: it genuinely depends on the stored state."""

        html = self.esegui(
            "return renderAvanzamentoPipeline();",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertIn('data-ricorda="ricalcolo"', html)
        self.assertNotIn("open", html.split("<summary>")[0].split("pipeline-details")[1])

    def test_due_ridisegni_di_fila_la_lasciano_aperta(self) -> None:
        """A faithful stand-in for the one-second rerender timer, without a DOM."""

        due = self.esegui(
            'state.aperti["ricalcolo"] = true;'
            "return [renderAvanzamentoPipeline(), renderAvanzamentoPipeline()];",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertTrue(all('data-ricorda="ricalcolo" open' in html for html in due))

    def test_il_gesto_scrive_aperto_e_scrive_chiuso(self) -> None:
        """The function the listener calls, exercised directly.

        On closing, the key stays and is `false`; it isn't deleted. A
        panel that defaults to open must distinguish "never touched" (falls
        back to its default) from "closed by the user" (stays closed on the
        next render) — deleting the key would collapse the two and reopen a
        panel the user just closed.
        """

        esito = self.esegui(
            "const finto = { dataset: { ricorda: 'ricalcolo' }, open: true };"
            "ricordaApertura(finto);"
            "const dopoApertura = state.aperti.ricalcolo === true;"
            "finto.open = false;"
            "ricordaApertura(finto);"
            'return { dopoApertura, dopoChiusura: state.aperti.ricalcolo, '
            'restaInMemoria: "ricalcolo" in state.aperti };',
            preparazione=self.con_confronto(pipeline=None),
        )

        self.assertTrue(esito["dopoApertura"])
        self.assertTrue(esito["restaInMemoria"])
        self.assertIs(esito["dopoChiusura"], False)

    def test_un_riquadro_chiuso_a_mano_resta_chiuso_al_ridisegno(self) -> None:
        """The rendering-side follow-up to the test above.

        It isn't enough for the stored state to say `false`: `apribile()`
        must read it. A panel that defaults to open, once closed, stays
        closed across repeated rerenders.
        """

        aperture = self.esegui(
            "const finto = { dataset: { ricorda: 'prova' }, open: false };"
            "ricordaApertura(finto);"
            "return [apribile('prova', true), apribile('prova', true), apribile('mai-toccato', true)];",
            preparazione=self.con_confronto(pipeline=None),
        )

        self.assertNotIn("open", aperture[0])
        self.assertNotIn("open", aperture[1])
        # One nobody has touched defaults to open.
        self.assertIn("open", aperture[2])

    def test_due_schede_diverse_non_si_aprono_insieme(self) -> None:
        """The storage key includes the product id: these are separate panels."""

        due = self.esegui(
            'state.aperti["composizione:p1"] = true;'
            "return [renderComponents(state.review.products[0]), renderComponents(state.review.products[1])];",
            preparazione=self.con_confronto(revisione={
                **REVISIONE,
                "products": [
                    {"id": "p1", "name": "Espositore uno", "itemType": "display", "quantity": 1,
                     "components": [{"name": "Pezzo", "ean": "1", "quantity": 6}],
                     "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
                    {"id": "p2", "name": "Espositore due", "itemType": "display", "quantity": 1,
                     "components": [{"name": "Pezzo", "ean": "2", "quantity": 6}],
                     "offers": [{"supplierId": "cipresso", "price": 1, "available": True}]},
                ],
            }, passo=2),
        )

        self.assertIn('data-ricorda="composizione:p1" open', due[0])
        self.assertIn('data-ricorda="composizione:p2"', due[1])
        self.assertNotIn("open", due[1])


class ProdottiCheNonSiTrovano(BancoDiProva):
    """A product exists, but the page answers "No product found".

    An excluded product doesn't appear under any filter except "Esclusi", and
    nothing on the search-with-no-results screen said so.
    """

    ELENCO = {
        **REVISIONE,
        "products": [
            {"id": "p1", "name": "KALIDERMA OLIO 300ML MANDORLE", "ean": "8009126851505",
             "quantity": 3, "excluded": True,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "p2", "name": "NEVAL BODY FLUIDA", "ean": "4009205823007", "quantity": 1,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "p3", "name": "Espositore misto", "ean": "", "itemType": "display", "quantity": 0,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "manual:1", "name": "Aggiunto a mano", "ean": "1", "addedManually": True,
             "quantity": 0, "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
        ],
    }

    # Excluded products live in a separate set, filled when the comparison
    # loads: prepared here the same way an `excluded: true` from the service
    # would populate it.
    ESCLUSI = 'state.excludedProductIds = new Set(["p1"]);'

    def pagina(self, ricerca: str) -> str:
        return self.esegui(
            f"state.filters.search = {json.dumps(ricerca)}; return renderQuantityStep();",
            preparazione=self.con_confronto(revisione=self.ELENCO, passo=2) + self.ESCLUSI,
        )

    def test_cercare_un_escluso_lo_dichiara_invece_di_dire_che_non_c_e(self) -> None:
        html = self.pagina("kaliderma")

        self.assertIn("fra gli esclusi", html)
        self.assertIn("KALIDERMA OLIO 300ML MANDORLE", html)
        self.assertIn("«Esclusi»", html)

    def test_una_ricerca_che_non_trova_niente_non_inventa_esclusi(self) -> None:
        html = self.pagina("zzz-non-esiste")

        self.assertNotIn("fra gli esclusi", html)
        self.assertIn("Nessun prodotto trovato", html)

    def test_senza_ricerca_la_pagina_non_dice_niente(self) -> None:
        html = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(revisione=self.ELENCO, passo=2) + self.ESCLUSI,
        )

        self.assertNotIn("fra gli esclusi", html)

    def test_il_totale_dice_da_dove_vengono_i_prodotti(self) -> None:
        """The total was correct but didn't say where the count came from."""

        html = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(revisione=self.ELENCO, passo=2) + self.ESCLUSI,
        )

        self.assertIn("2 dall’elenco del gestionale", html)
        self.assertIn("1 espositore dei listini", html)
        self.assertIn("1 aggiunto a mano", html)


class IlNumeroCheDeveTornareColGestionale(BancoDiProva):
    """The Continue count must match the management-software document's row count.

    The Continue button was counting the whole list — management-software
    export plus price-list displays plus manually added products — while the
    document card showed a different number, with nothing explaining the gap.
    Two numbers that don't match make the user suspect the program read the
    wrong file.

    Exercised tests: call the real `comandoContinua` and `renderNumeriPipeline`.
    """

    ELENCO = {
        **REVISIONE,
        "products": [
            {"id": "p1", "name": "KALIDERMA", "ean": "8009126851505", "quantity": 1,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "p2", "name": "NEVAL BODY", "ean": "4009205823007", "quantity": 1,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "p3", "name": "Espositore misto", "ean": "", "itemType": "display", "quantity": 0,
             "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
            {"id": "manual:1", "name": "Aggiunto a mano", "ean": "1", "addedManually": True,
             "quantity": 0, "offers": [{"supplierId": "cipresso", "price": 2, "available": True}]},
        ],
    }

    def test_il_pulsante_conta_i_prodotti_del_gestionale_non_tutto_l_elenco(self) -> None:
        continua = self.esegui(
            "return comandoContinua();",
            preparazione=self.con_confronto(
                revisione=self.ELENCO,
                pipeline={"stato": "COMPLETATO", "messaggio": "Confronto aggiornato.", "fasi": []},
            ),
        )

        # Four products in the list, two from the management software: the
        # button shows the two.
        self.assertEqual(continua["etichetta"], "Continua con 2 prodotti del gestionale →")

    def test_il_pulsante_dice_che_quel_numero_e_del_gestionale(self) -> None:
        # Without "del gestionale" the lower number would look like a halved
        # list instead of the part that must match the paper document.
        continua = self.esegui(
            "return comandoContinua();",
            preparazione=self.con_confronto(
                revisione=self.ELENCO,
                pipeline={"stato": "COMPLETATO", "messaggio": "Confronto aggiornato.", "fasi": []},
            ),
        )

        self.assertIn("del gestionale", continua["etichetta"])

    def test_i_numeri_del_ricalcolo_dicono_quanti_vengono_dal_gestionale(self) -> None:
        html = self.esegui(
            'return renderNumeriPipeline({ documenti: 5, fornitori: 4, prodotti: 457, prodottiGestionale: 451 });',
            preparazione=self.con_confronto(),
        )

        self.assertIn("457 prodotti, di cui 451 dal gestionale", html)

    def test_quando_i_due_numeri_coincidono_se_ne_scrive_uno_solo(self) -> None:
        # A second number equal to the first adds nothing and is dropped: copy
        # is written only when it tells the reader something new.
        html = self.esegui(
            'return renderNumeriPipeline({ documenti: 5, fornitori: 4, prodotti: 451, prodottiGestionale: 451 });',
            preparazione=self.con_confronto(),
        )

        self.assertIn("451 prodotti", html)
        self.assertNotIn("dal gestionale", html)


class QualiColonneLegge(BancoDiProva):
    """Shows which columns of each document were read for what.

    Exercised tests: calls the real `renderFileCard` with real state.
    """

    COLONNE = """
      state.colonneDocumenti = {
        caricate: true, caricando: false, runId: "run-1", motivo: "", errore: "",
        perNome: {
          "listino.xlsx": {
            fileName: "listino.xlsx", role: "supplier", adapterId: "betulla_v1",
            sheet: "Sheet1", headerRow: 1, dataStartRow: 2, origin: "registro",
            orderColumn: { lettera: "C", colonna: 3, intestazione: "ORDINE", trovata: true },
            columns: [
              { campo: "ean", etichetta: "Codice a barre (EAN)", colonna: 1, lettera: "A",
                intestazione: "EAN", esempio: "8000000000001", dichiarata: "EAN", trovata: true },
              { campo: "unit_price_net", etichetta: "Prezzo netto", colonna: 6, lettera: "F",
                intestazione: "Cessione", esempio: "3,98", dichiarata: "Cessione", trovata: true }
            ]
          }
        }
      };
    """

    def scheda(self, extra: str = "") -> str:
        return self.esegui(
            'return renderFileCard({ id: "f1", name: "listino.xlsx", kind: "Listino", '
            'supplier: "BETULLA", status: "ready", schemaState: "SCHEMA_NOTO", rows: 10, message: "" });',
            preparazione=self.con_confronto() + self.COLONNE + extra,
        )

    def test_la_scheda_dice_da_quale_colonna_arriva_il_prezzo(self) -> None:
        html = self.scheda()

        self.assertIn("Quali colonne leggo", html)
        self.assertIn("Cessione", html)
        self.assertIn("Prezzo netto", html)
        self.assertIn("3,98", html)

    def test_dice_dove_finisce_l_ordine_e_da_dove_viene_l_assegnazione(self) -> None:
        html = self.scheda()

        self.assertIn("L’ordine viene scritto nella colonna", html)
        self.assertIn("Assegnazione dichiarata dal registro dei fornitori.", html)

    def test_una_colonna_promessa_e_non_trovata_si_vede(self) -> None:
        html = self.scheda("""
          state.colonneDocumenti.perNome["listino.xlsx"].columns.push({
            campo: "pieces_per_carton", etichetta: "Pezzi per collo", colonna: null,
            lettera: "", intestazione: "", esempio: "", dichiarata: "PzCt", trovata: false });
        """)

        self.assertIn("is-missing", html)
        self.assertIn("non trovata nel documento", html)
        self.assertIn("PzCt", html)

    def test_il_riquadro_si_ricorda_di_essere_aperto(self) -> None:
        # `render()` rewrites the innerHTML every second during a recalculation:
        # without `apribile()` the panel would collapse under the user's hands.
        html = self.scheda('state.aperti["colonne-documento:listino.xlsx"] = true;')

        self.assertIn('data-ricorda="colonne-documento:listino.xlsx"', html)
        self.assertIn("open", html)

    def test_finche_non_si_sa_niente_la_scheda_resta_come_prima(self) -> None:
        html = self.esegui(
            'return renderFileCard({ id: "f1", name: "altro.xlsx", kind: "Listino", '
            'supplier: "BETULLA", status: "ready", schemaState: "SCHEMA_NOTO", rows: 10, message: "" });',
            preparazione=self.con_confronto()
            + 'state.colonneDocumenti = { caricate: false, caricando: true, runId: "", perNome: {}, motivo: "", errore: "" };',
        )

        self.assertNotIn("Quali colonne leggo", html)

    def test_un_documento_senza_risposta_lo_dice_invece_di_sparire(self) -> None:
        html = self.esegui(
            'return renderFileCard({ id: "f1", name: "altro.xlsx", kind: "Listino", '
            'supplier: "BETULLA", status: "ready", schemaState: "SCHEMA_NOTO", rows: 10, message: "" });',
            preparazione=self.con_confronto()
            + 'state.colonneDocumenti = { caricate: true, caricando: false, runId: "run-1", perNome: {}, '
              'motivo: "La cartella di quel ricalcolo non c\'e\' piu\'.", errore: "" };',
        )

        self.assertIn("Quali colonne leggo", html)
        self.assertIn("La cartella di quel ricalcolo non c", html)


class DoveCominicianoIProdotti(BancoDiProva):
    """States in the page: "products start after the row that says LISTINO".

    The service already resolves this rule on every read (`data_start_marker`);
    the page only accepts a typed row number, which doesn't survive the next
    week's file: a promotional block can precede the real price list at a
    different length each time, so a hardcoded row number would silently cut
    in the wrong place. Declaring the marker text instead
    ("after the row that says X") keeps working when the block shifts.

    Exercised tests: calls the real `renderSchemaDocument`,
    `schemaApplicaSeparatore` and `schemaMappingPayload`, with real state.
    """

    PENDENTI = {
        "ok": True,
        "required": True,
        "runId": "run-1",
        "suppliers": [{"id": "betulla", "name": "BETULLA"}],
        "documents": [{
            "profileId": "p-quercia",
            "fileName": "LISTINO QUERCIA.xlsx",
            "format": "xlsx",
            "sizeBytes": 1024,
            "sheets": [{
                "name": "Foglio1",
                "maxRow": 4190,
                "maxColumn": 8,
                "headerRows": [6],
                "rows": [
                    {"row": 6, "values": ["COD", "EAN", "DESCRIZIONE", "PZ", "PREZZO"]},
                    {"row": 7, "values": ["1", "8000000000001", "OMAGGIO UNO", "6", "0,01"]},
                    {"row": 67, "values": ["2", "8000000000002", "OMAGGIO DUE", "6", "0,01"]},
                    {"row": 68, "values": ["LISTINO"]},
                    {"row": 69, "values": ["3", "8000000000003", "PRODOTTO VERO", "6", "1,52"]},
                    {"row": 70, "values": ["4", "8000000000004", "ALTRO PRODOTTO", "6", "2,10"]},
                ],
                "sectionBreaks": [
                    {"row": 68, "column": 1, "letter": "A", "text": "LISTINO", "data_from": 69},
                ],
                "columns": [],
            }],
            "suggestion": {
                "role": "supplier",
                "supplierId": "",
                "supplierName": "",
                "sheet": "Foglio1",
                "headerRow": 6,
                "dataStartRow": 7,
                "columns": {"description": 3, "unit_price_net": 5, "ean": 2, "pieces_per_carton": 4},
                "orderColumn": 6,
                "match": 0.2,
            },
        }],
    }

    def preparazione(self, extra: str = "") -> str:
        return (
            self.con_confronto()
            + f"initializeSchemaMapping({json.dumps(self.PENDENTI)});"
            + extra
        )

    def scelto(self, testo: str | None = None) -> str:
        """State after picking the separator, applied by the real function."""

        cambio = "" if testo is None else f'valore.markerText = {json.dumps(testo)};'
        return f"""
          const valore = state.schemaMapping.values["p-quercia"];
          valore.dataStartBreak = "68";
          schemaApplicaSeparatore(valore, schemaSelectedSheet(state.schemaMapping.data.documents[0], valore));
          {cambio}
        """

    def test_la_regola_si_puo_scegliere_invece_di_digitare_un_numero(self) -> None:
        html = self.esegui(
            "return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(),
        )

        self.assertIn("I prodotti cominciano", html)
        self.assertIn('data-schema-field="dataStartBreak"', html)
        self.assertIn("Dopo la riga 68 («LISTINO»)", html)

    def test_scegliere_il_separatore_detta_la_prima_riga_dei_prodotti(self) -> None:
        # Without this rule the row number can only be guessed by opening the
        # spreadsheet.
        valore = self.esegui(
            self.scelto() + "return { riga: valore.dataStartRow, testo: valore.markerText };",
            preparazione=self.preparazione(),
        )

        self.assertEqual(valore["riga"], 69)
        self.assertEqual(valore["testo"], "LISTINO")

    def test_la_regola_arriva_al_servizio_al_posto_del_solo_numero(self) -> None:
        payload = self.esegui(
            self.scelto() + "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )
        mappatura = payload["mappings"][0]

        self.assertEqual(mappatura["dataStartRow"], 69)
        self.assertEqual(mappatura["dataStartMarker"], {
            "column": 1, "match": "equals", "text": "LISTINO", "offset": 1,
        })

    def test_senza_separatore_scelto_non_si_manda_nessuna_regola(self) -> None:
        # The field is omitted: `{}` would make the service reject the mapping,
        # and a rule invented by the program would be worse than a raw number.
        payload = self.esegui(
            "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )

        self.assertNotIn("dataStartMarker", payload["mappings"][0])

    def test_un_testo_accorciato_diventa_una_regola_che_contiene(self) -> None:
        # Real separator text carries the week's date range inside it: the
        # stable part must be matched with "contains", not exact equality.
        payload = self.esegui(
            self.scelto("LIST") + "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )

        self.assertEqual(payload["mappings"][0]["dataStartMarker"], {
            "column": 1, "match": "contains", "text": "LIST", "offset": 1,
        })

    def test_un_separatore_accorciato_dal_profilo_non_chiede_l_uguaglianza(self) -> None:
        # Most separator rows are long promotional text, truncated to a fixed
        # length with an ellipsis. "Equals" on truncated text is rejected by
        # the service, which compares against the full cell.
        payload = self.esegui(
            """
              const valore = state.schemaMapping.values["p-quercia"];
              state.schemaMapping.data.documents[0].sheets[0].sectionBreaks = [
                { row: 10, column: 1, letter: "A", text: "OGNI 1500,00 OMAGGIO A SCELTA 1X24 RAZZ ELETTRICO RICARICA 60 NOTTI 2,80…", data_from: 11 }
              ];
              state.schemaMapping.data.documents[0].sheets[0].rows.push({ row: 10, values: ["OGNI 1500"] });
              state.schemaMapping.data.documents[0].sheets[0].rows.push({ row: 11, values: ["x"] });
              valore.dataStartBreak = "10";
              schemaApplicaSeparatore(valore, schemaSelectedSheet(state.schemaMapping.data.documents[0], valore));
              return schemaMappingPayload();
            """,
            preparazione=self.preparazione(),
        )
        marcatore = payload["mappings"][0]["dataStartMarker"]

        self.assertEqual(marcatore["match"], "contains")
        self.assertNotIn("…", marcatore["text"])
        self.assertTrue(marcatore["text"].endswith("2,80"))

    def test_l_anteprima_fa_vedere_dove_taglia(self) -> None:
        # The real bug: typing 69 kept showing the promotional block's rows,
        # never surfacing the cutoff row itself.
        html = self.esegui(
            self.scelto() + "return renderSchemaPreview(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(),
        )

        self.assertIn("is-break", html)
        self.assertIn("PRODOTTO VERO", html)

    def test_il_numero_non_si_scrive_a_mano_finche_c_e_la_regola(self) -> None:
        # Two sources for the same row number is how a mismatched rule — one
        # describing a different row than the one declared — reaches the
        # service.
        html = self.esegui(
            self.scelto() + "return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(),
        )

        self.assertIn("readonly", html)

    def test_un_documento_senza_separatori_resta_come_prima(self) -> None:
        html = self.esegui(
            "return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(
                'state.schemaMapping.data.documents[0].sheets[0].sectionBreaks = [];'
            ),
        )

        self.assertIn("Prima riga dei prodotti", html)
        self.assertNotIn('data-schema-field="dataStartBreak"', html)


class LaPaginaUnoNonSiRipete(BancoDiProva):
    """Bugs measured against the live page: repeated or unnecessary copy.

    All the same family: the same thing written twice on the same screen, or
    written when it serves no one. Exercised tests.
    """

    # The same warning can arrive from two places: inside the comparison
    # (`warnings`) and inside the pipeline status (`avvisi`).
    # QUANTITA_RIPRESE_DAL_GESTIONALE sends it through both.
    AVVISO = {
        "code": "QUANTITA_RIPRESE_DAL_GESTIONALE",
        "severity": "warning",
        "blocking": False,
        "title": "Le quantità sono quelle dell'elenco",
        "message": "415 quantità sono state riprese dall'elenco del gestionale.",
    }

    COMPLETATO = {
        "stato": "COMPLETATO",
        "messaggio": "Confronto aggiornato: 457 prodotti, 4 fornitori.",
        "fasi": [],
        "numeri": {"documenti": 5, "fornitori": 4, "prodotti": 457, "casiSemantici": 871, "casiDecisi": 870},
        "avvisi": [AVVISO],
    }

    def revisione(self, **extra) -> dict:
        return {**REVISIONE, **extra}

    def pagina(self, pipeline: dict | None, *, revisione: dict | None = None) -> str:
        return self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=pipeline, revisione=revisione or REVISIONE),
        )

    def test_lo_stesso_avviso_non_si_stampa_due_volte(self) -> None:
        """One in the top panel, one next to the pipeline's own result."""

        html = self.pagina(self.COMPLETATO, revisione=self.revisione(warnings=[self.AVVISO]))

        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_un_avviso_che_solo_la_catena_conosce_si_vede(self) -> None:
        """Deduplication removes duplicates, not distinct warnings."""

        solo_della_catena = {**self.AVVISO, "code": "COMPILAZIONE_DA_RICONFIGURARE",
                             "title": "La compilazione va ricontrollata"}
        pipeline = {**self.COMPLETATO, "avvisi": [self.AVVISO, solo_della_catena]}
        html = self.pagina(pipeline, revisione=self.revisione(warnings=[self.AVVISO]))

        self.assertIn("La compilazione va ricontrollata", html)
        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_senza_riquadro_in_cima_l_avviso_della_catena_resta(self) -> None:
        """If the comparison doesn't carry it, the pipeline status is the only place it reads."""

        html = self.pagina(self.COMPLETATO, revisione=self.revisione(warnings=[]))

        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_la_riga_tecnica_dello_schema_non_si_legge_quando_va_tutto_bene(self) -> None:
        """Repeated across every document card: the schema-recognition detail line."""

        documenti = self.revisione(files=[
            {"name": "ordine2.xlsx", "kind": "Gestionale", "supplier": "Gestionale", "status": "ready",
             "message": "Schema riconosciuto dal registro (gestionale_v1, confidenza 0.99): nessuna chiamata AI."},
            {"name": "listino.xlsx", "kind": "Listino", "supplier": "LARICE", "status": "ready",
             "message": "Schema riconosciuto dal registro (larice_v1, confidenza 0.97): nessuna chiamata AI."},
        ])
        html = self.pagina(self.COMPLETATO, revisione=documenti)

        self.assertNotIn("Schema riconosciuto dal registro", html)
        # The documents stay, with their outcome.
        self.assertIn("ordine2.xlsx", html)
        self.assertIn("listino.xlsx", html)

    def test_ma_quando_qualcosa_non_va_la_motivazione_si_legge(self) -> None:
        documenti = self.revisione(files=[
            {"name": "sconosciuto.xlsx", "kind": "Listino", "supplier": "Da riconoscere", "status": "warning",
             "message": "Lo schema somiglia a larice_v1 ma due colonne non tornano."},
        ])
        html = self.pagina(self.COMPLETATO, revisione=documenti)

        self.assertIn("Lo schema somiglia a larice_v1 ma due colonne non tornano.", html)

    def test_la_scheda_del_gestionale_non_dice_due_volte_gestionale(self) -> None:
        documenti = self.revisione(files=[
            {"name": "ordine2.xlsx", "kind": "Gestionale", "supplier": "Gestionale", "status": "ready", "rows": 451},
        ])
        html = self.pagina(self.COMPLETATO, revisione=documenti)
        scheda = html.split('class="file-card"', 1)[1].split("</article>", 1)[0]

        self.assertEqual(scheda.count("Gestionale"), 1)
        self.assertIn("451 righe", scheda)

    def test_il_listino_tiene_fornitore_e_tipo_perche_sono_due_cose(self) -> None:
        documenti = self.revisione(files=[
            {"name": "listino.xlsx", "kind": "Listino", "supplier": "LARICE", "status": "ready", "rows": 6333},
        ])
        html = self.pagina(self.COMPLETATO, revisione=documenti)
        scheda = html.split('class="file-card"', 1)[1].split("</article>", 1)[0]

        self.assertIn("Listino", scheda)
        self.assertIn("LARICE", scheda)

    def test_il_titolo_del_riquadro_non_e_il_testo_del_pulsante(self) -> None:
        """The panel title must not repeat the button's own label."""

        html = self.pagina(self.COMPLETATO)

        self.assertIn("<h3>Confronto dei listini</h3>", html)
        self.assertEqual(html.count("Rifai il confronto"), 1)

    def test_il_messaggio_che_dice_che_e_andato_bene_non_si_scrive(self) -> None:
        html = self.pagina(self.COMPLETATO)

        self.assertNotIn("Confronto aggiornato: 457 prodotti", html)
        # The run's own numbers stay, and are what's actually looked at.
        self.assertIn("5 documenti", html)
        self.assertIn("4 fornitori", html)
        # "prodotti" stays on purpose: it's the number that must match the
        # document card's row count (`comandoContinua` and `prodottiGestionale`).
        self.assertIn("457 prodotti", html)
        # "cases to review" and "decided" already live inside "Recalculation
        # details", with more context there.
        self.assertNotIn("casi da valutare", html)
        self.assertNotIn("870 decisi", html)

    def test_mentre_gira_il_messaggio_e_l_unica_cosa_che_dice_a_che_punto_e(self) -> None:
        html = self.pagina({
            "stato": "IN_CORSO",
            "messaggio": "Lettura dei listini…",
            "fasi": [],
            "numeri": {"documenti": 5},
            "avanzamento": {"percento": 40},
        })

        self.assertIn("Lettura dei listini…", html)

    def test_una_catena_fermata_dice_sempre_com_e_andata(self) -> None:
        html = self.pagina({
            "stato": "ERRORE",
            "messaggio": "Il ricalcolo non è arrivato in fondo.",
            "fasi": [],
            "numeri": {"documenti": 5, "fornitori": 4},
            "fermata": {"code": "ALTRO", "message": "Il listino BETULLA non si legge."},
        })

        self.assertIn("Il ricalcolo non è arrivato in fondo.", html)
        self.assertIn("Il listino BETULLA non si legge.", html)

    def test_quando_si_ferma_dice_che_il_confronto_di_prima_e_ancora_li(self) -> None:
        """This line belongs here, once, instead of repeated under the button."""

        html = self.pagina({
            "stato": "ERRORE", "messaggio": "", "fasi": [],
            "fermata": {"code": "ALTRO", "message": "Il listino BETULLA non si legge."},
        })

        self.assertIn("Il confronto di prima è ancora al suo posto.", html)

    def test_alla_prima_volta_non_c_e_nessun_confronto_da_salvare(self) -> None:
        prima_volta = self.revisione(products=[])
        html = self.pagina({
            "stato": "ERRORE", "messaggio": "", "fasi": [],
            "fermata": {"code": "ALTRO", "message": "Il listino BETULLA non si legge."},
        }, revisione=prima_volta)

        self.assertNotIn("Il confronto di prima è ancora al suo posto.", html)

    def test_in_fondo_alla_pagina_c_e_un_comando_solo(self) -> None:
        """Settings isn't a workflow step and doesn't belong among the page actions."""

        html = self.pagina(self.COMPLETATO)
        coda = html.split('<div class="page-actions">', 1)[1]

        self.assertEqual(coda.count("<button"), 1)
        self.assertIn('data-action="next"', coda)
        # The command hasn't disappeared: it moved to the top of the page.
        testa = html.split('<div class="page-heading">', 1)[1].split("</div>\n    </div>", 1)[0]
        self.assertIn('data-action="apri-impostazioni"', testa)

    def test_la_spiegazione_del_ricalcolo_si_legge_solo_la_prima_volta(self) -> None:
        prima_volta = self.pagina(None, revisione=self.revisione(products=[]))
        self.assertIn("ci vogliono alcuni minuti", prima_volta)

        dopo = self.pagina(self.COMPLETATO)
        self.assertNotIn("ci vogliono alcuni minuti", dopo)

    def test_a_ricalcolo_finito_non_resta_una_barra_piena(self) -> None:
        """A full progress bar is the "it worked" message drawn instead of written."""

        finito = self.pagina(self.COMPLETATO)
        self.assertNotIn("pipeline-progress__bar", finito)

        mentre_gira = self.pagina({
            "stato": "IN_CORSO", "messaggio": "Lettura dei listini…", "fasi": [],
            "avanzamento": {"percento": 40},
        })
        self.assertIn("pipeline-progress__bar", mentre_gira)
        self.assertIn("width: 40%", mentre_gira)

    def test_una_catena_fermata_tiene_la_barra_perche_dice_dov_e_arrivata(self) -> None:
        html = self.pagina({
            "stato": "ERRORE", "messaggio": "", "fasi": [],
            "avanzamento": {"percento": 70},
            "fermata": {"code": "ALTRO", "message": "Il listino BETULLA non si legge."},
        })

        self.assertIn("width: 70%", html)

    def test_il_comando_del_ricalcolo_sta_nel_suo_riquadro(self) -> None:
        """Alone at the bottom of the panel it looked like the page's own primary button."""

        html = self.pagina(self.COMPLETATO)
        intestazione = html.split('<section class="panel import-panel pipeline-panel">', 1)[1]
        intestazione = intestazione.split('class="panel__header"', 1)[1].split("</div>\n      </div>", 1)[0]

        self.assertIn('data-action="avvia-pipeline"', intestazione)


class IlConfrontoDeiFornitoriEUnaTabella(BancoDiProva):
    """Page 2: the same four labels repeated inside every supplier card.

    On a page with many products, hundreds of labels had to be read to read
    the numbers next to them, and the numbers themselves weren't aligned in
    columns. Exercised tests.
    """

    PROMOZIONE = {
        "kind": "soglia_omaggio",
        "reward": {"description": "RESALINA SALE LAVASTOVIGLIE KG1"},
        "state": {
            "status": "vicina",
            "message": (
                "10 cartoni fra i 133 prodotti dell'offerta = 1 cartone di "
                "RESALINA SALE LAVASTOVIGLIE KG1 in omaggio: te ne manca 1 per il quinto."
            ),
        },
    }

    ELENCO = {
        **REVISIONE,
        "suppliers": [
            {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
            {"id": "larice", "name": "LARICE", "minimumOrder": 0},
            {"id": "noce", "name": "NOCE", "minimumOrder": 0},
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 0},
        ],
        "products": [{
            "id": "p1",
            "name": "GILLARDO VENEX RICAMBI",
            "ean": "8009408415265",
            "quantity": 2,
            "lastUnitPrice": 7.43,
            "selectedSupplierId": "larice",
            "offers": [
                {"supplierId": "betulla", "price": 89.5, "unitsPerOrderUnit": 10,
                 "pricePerPiece": 8.95, "available": True, "matchStatus": "EAN esatto"},
                {"supplierId": "larice", "price": 95.2, "unitsPerOrderUnit": 10,
                 "pricePerPiece": 9.52, "available": True, "matchStatus": "EAN esatto",
                 "promotions": [PROMOZIONE]},
                {"supplierId": "noce", "available": False},
                {"supplierId": "cipresso", "available": False},
            ],
        }],
    }

    SENZA_NESSUNA_OFFERTA = {
        **ELENCO,
        "products": [{
            "id": "p1", "name": "PARIGINOTA SOLVENTE", "ean": "8009614311818", "quantity": 0,
            "offers": [
                {"supplierId": "betulla", "available": False},
                {"supplierId": "larice", "available": False},
                {"supplierId": "noce", "available": False},
                {"supplierId": "cipresso", "available": False},
            ],
        }],
    }

    def scheda(self, revisione: dict | None = None) -> str:
        return self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione or self.ELENCO, passo=2),
        )

    def test_le_intestazioni_si_scrivono_una_volta_sola(self) -> None:
        html = self.scheda()

        self.assertIn("offer-grid__head", html)
        # Two available offers, one header per column.
        self.assertEqual(html.count("Prezzo per pezzo"), 3, "una in testa più una per riga, come etichetta nascosta")
        self.assertEqual(html.count("<span>Prezzo per pezzo</span>"), 1)

    def test_i_numeri_di_ogni_fornitore_stanno_in_una_riga(self) -> None:
        html = self.scheda()
        righe = html.count('<label class="offer-card')

        self.assertEqual(righe, 2)
        self.assertIn("89,50", html)
        self.assertIn("95,20", html)

    def test_le_etichette_restano_per_chi_legge_con_la_voce(self) -> None:
        """Hidden by the stylesheet, not removed from the markup."""

        html = self.scheda()

        self.assertIn('class="offer-card__label">Prezzo per pezzo</span>', html)
        self.assertIn(".offer-card__label", STYLES.read_text(encoding="utf-8"))

    def test_i_fornitori_che_non_hanno_il_prodotto_stanno_in_una_riga_sola(self) -> None:
        html = self.scheda()

        self.assertEqual(html.count("non ce l’hanno nel listino di adesso"), 1)
        self.assertIn("NOCE e CIPRESSO: non ce l’hanno nel listino di adesso.", html)

    def test_un_prodotto_senza_nessuna_offerta_non_riempie_la_scheda(self) -> None:
        """A product with no offers at all gets one combined message, not four."""

        html = self.scheda(self.SENZA_NESSUNA_OFFERTA)

        self.assertEqual(html.count("non ce l’hanno nel listino di adesso"), 1)
        self.assertIn("BETULLA, LARICE, NOCE e CIPRESSO", html)
        # And no headers: there's no column left to label.
        self.assertNotIn("offer-grid__head", html)

    def test_un_fornitore_con_un_motivo_suo_lo_tiene(self) -> None:
        """Grouping must not swallow a supplier-specific warning."""

        revisione = {
            **self.ELENCO,
            "products": [{
                **self.ELENCO["products"][0],
                "offers": [
                    *self.ELENCO["products"][0]["offers"][:2],
                    {"supplierId": "noce", "available": False,
                     "warning": "La riga del listino non ha i pezzi per collo."},
                    {"supplierId": "cipresso", "available": False},
                ],
            }],
        }
        html = self.scheda(revisione)

        self.assertIn("La riga del listino non ha i pezzi per collo.", html)
        self.assertIn("CIPRESSO: non ce l’hanno nel listino di adesso.", html)
        self.assertNotIn("NOCE e CIPRESSO", html)

    def test_l_offerta_non_ripete_il_nome_dell_omaggio(self) -> None:
        """Avoids repeating the free-goods item's name in two adjacent phrases."""

        html = self.scheda()

        self.assertEqual(html.count("RESALINA SALE LAVASTOVIGLIE KG1"), 1)
        self.assertNotIn("Omaggio: RESALINA", html)

    def test_ma_un_omaggio_che_il_messaggio_non_nomina_si_dice(self) -> None:
        promozione = {
            **self.PROMOZIONE,
            "state": {"status": "vicina", "message": "Ti manca 1 cartone per il prossimo omaggio."},
        }
        testo = self.esegui(f"return promotionText({json.dumps(promozione)});")

        self.assertEqual(
            testo,
            "Ti manca 1 cartone per il prossimo omaggio. Omaggio: RESALINA SALE LAVASTOVIGLIE KG1",
        )

    def test_il_titolo_del_confronto_non_si_ripete_su_ogni_scheda(self) -> None:
        html = self.scheda()

        self.assertNotIn("Confronto dei fornitori", html)
        self.assertIn("<span>Fornitore</span>", html)

    def test_il_plurale_e_giusto(self) -> None:
        """Singular/plural agreement on counted labels."""

        una_sola = {
            **self.ELENCO,
            "products": [{
                **self.ELENCO["products"][0],
                "offers": [self.ELENCO["products"][0]["offers"][0], {"supplierId": "larice", "available": False}],
                "selectedSupplierId": "betulla",
            }],
        }
        html = self.scheda(una_sola)

        # "offerta" is ambiguous in a retail context (it also means "discount");
        # the count is of distinct suppliers carrying the product, not of
        # price-list rows, so the label says "fornitore" instead.
        self.assertIn("<span>1 fornitore ce l’ha</span>", html)
        self.assertNotIn("1 fornitori", html)
        self.assertNotIn("offerta</span>", html)

    def test_il_totale_dell_ordine_sta_nella_fascia_che_resta_visibile(self) -> None:
        """Stays visible in the sticky totals strip, not in the scrolling list header."""

        pagina = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=self.ELENCO, passo=2),
        )

        self.assertIn("supplier-totals-strip__item--totale", pagina)
        self.assertNotIn("Totale provvisorio", pagina)
        # Same number as before: the sum across suppliers. One product only,
        # so the order total equals that row's total.
        fascia = pagina.split("supplier-totals-strip__item--totale", 1)[1].split("</span>", 3)
        self.assertIn("190,40", "".join(fascia))


class GliAvvisiUgualiSiContano(BancoDiProva):
    """Page 3: dozens of panels stood between the user and the order totals.

    Many blocking issues and many warnings, printed one by one, but sharing
    only a handful of distinct titles: what repeated was the title, while
    each message underneath is different and needed. Exercised tests.
    """

    def avvisi(self, quanti: int, *, titolo: str = "Possibile prodotto CIPRESSO", bloccante: bool = False) -> list:
        return [{
            "id": f"a{indice}",
            "title": titolo,
            "message": f"CIPRESSO propone «PRODOTTO {indice}». Indica se è lo stesso prodotto.",
            "severity": "error" if bloccante else "warning",
            "blocking": bloccante,
        } for indice in range(1, quanti + 1)]

    def html(self, avvisi: list, *, preparazione: str = "") -> str:
        return self.esegui(
            f"return renderAvvisi({json.dumps(avvisi)}.map((voce) => normalizeIssue(voce, voce.id)), 'prova');",
            preparazione=preparazione or self.con_confronto(pipeline=None, passo=3),
        )

    def test_tre_avvisi_uguali_diventano_un_riquadro_solo(self) -> None:
        html = self.html(self.avvisi(3))

        self.assertEqual(html.count("alert--gruppo"), 1)
        # Only once on the page: the second occurrence is the open-state
        # storage key, which nobody reads as text.
        self.assertEqual(html.count("<strong>Possibile prodotto CIPRESSO</strong>"), 1)

    def test_e_nessun_messaggio_va_perso(self) -> None:
        html = self.html(self.avvisi(8))

        for indice in range(1, 9):
            self.assertIn(f"«PRODOTTO {indice}»", html)
        self.assertEqual(html.count("<li>"), 8)

    def test_il_numero_dice_quanti_sono(self) -> None:
        html = self.html(self.avvisi(8))

        self.assertIn(">8</span>", html)

    def test_due_avvisi_uguali_restano_due_riquadri(self) -> None:
        """Below the grouping threshold, grouping would add a click for no benefit."""

        html = self.html(self.avvisi(2))

        self.assertNotIn("alert--gruppo", html)
        self.assertEqual(html.count("<strong>Possibile prodotto CIPRESSO</strong>"), 2)

    def test_titoli_diversi_non_si_mescolano(self) -> None:
        misti = [*self.avvisi(3), *self.avvisi(3, titolo="Possibile prodotto NOCE")]
        html = self.html(misti)

        self.assertEqual(html.count("alert--gruppo"), 2)
        self.assertIn("Possibile prodotto CIPRESSO", html)
        self.assertIn("Possibile prodotto NOCE", html)

    def test_un_gruppo_bloccante_non_si_puo_chiudere(self) -> None:
        """This is why compilation won't start: it must not be collapsible."""

        html = self.html(self.avvisi(4, titolo="Conferma richiesta", bloccante=True))

        self.assertIn('<div class="alert alert--danger alert--gruppo" role="alert">', html)
        self.assertNotIn("<details", html)
        self.assertIn("Conferma richiesta · bloccante", html)

    def test_un_gruppo_di_avvisi_parte_chiuso_e_ricorda_l_apertura(self) -> None:
        """Non-blocking and numerous: closed by default, and `apribile()` remembers if opened."""

        chiuso = self.html(self.avvisi(4))
        self.assertIn("<details", chiuso)
        self.assertIn('data-ricorda="avvisi:prova:Possibile prodotto CIPRESSO"', chiuso)
        self.assertNotIn(" open>", chiuso)

        aperto = self.html(
            self.avvisi(4),
            preparazione=self.con_confronto(pipeline=None, passo=3)
            + 'state.aperti["avvisi:prova:Possibile prodotto CIPRESSO"] = true;',
        )
        self.assertIn('data-ricorda="avvisi:prova:Possibile prodotto CIPRESSO" open', aperto)

    def test_vale_anche_per_il_riquadro_in_cima_alle_pagine_uno_e_due(self) -> None:
        """Several price lists with the same problem are one grouped warning, not several."""

        ripetuti = [{
            "id": f"d{indice}",
            "title": "Listino duplicato",
            "message": f"Del fornitore FORNITORE{indice} ci sono due listini: uso il più recente.",
            "severity": "warning",
        } for indice in range(1, 5)]
        html = self.esegui(
            "return renderSourceWarnings();",
            preparazione=self.con_confronto(
                pipeline=None,
                revisione={**REVISIONE, "warnings": ripetuti},
            ),
        )

        self.assertEqual(html.count("<strong>Listino duplicato</strong>"), 1)
        self.assertIn("alert--gruppo", html)
        self.assertEqual(html.count("<li>"), 4)
        self.assertIn("4 avvisi", html)


class LaPaginaTreHaUnComandoSolo(BancoDiProva):
    """The final confirmation button vs. a secondary action stealing its focus. Exercised tests."""

    ELENCO = {
        **REVISIONE,
        "suppliers": [
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 1000},
            {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
        ],
        "products": [{
            "id": "p1",
            "name": "Prodotto uno",
            "quantity": 3,
            "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
        }],
    }

    def pagina(self, revisione: dict | None = None, compileResult: dict | None = None) -> str:
        return self.esegui(
            f"state.compileResult = {json.dumps(compileResult)}; return renderCompileStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione or self.ELENCO, passo=3),
        )

    def test_prima_del_riepilogo_gli_avvisi_uguali_sono_un_riquadro(self) -> None:
        """The acceptance criterion: dozens of panels collapse to a handful.

        Shown small here — a handful of warnings with the same title —
        because it's the same rule: what repeated was the title, not the
        message.
        """

        molti = {
            **self.ELENCO,
            "warnings": [{
                "id": f"w{indice}",
                "title": "Possibile prodotto CIPRESSO",
                "message": f"CIPRESSO propone «PRODOTTO {indice}». Indica se è lo stesso prodotto.",
                "severity": "warning",
                "blocking": False,
            } for indice in range(1, 9)],
        }
        html = self.pagina(molti)
        riepilogo = html.split("<h3>Ricontrolla prima di compilare</h3>", 1)[1].split("</section>", 1)[0]

        self.assertEqual(riepilogo.count("<strong>Possibile prodotto CIPRESSO</strong>"), 1)
        self.assertIn("alert--gruppo", riepilogo)
        # And no message is lost.
        for indice in range(1, 9):
            self.assertIn(f"«PRODOTTO {indice}»", riepilogo)

    def test_un_solo_pulsante_primario(self) -> None:
        html = self.pagina()

        self.assertEqual(html.count("button--primary"), 1)
        self.assertIn('data-action="compile"', html.split("button--primary", 1)[1][:200])

    def test_dopo_la_compilazione_il_primario_diventa_lo_scarico(self) -> None:
        """After a successful run, the primary button switches to the actual
        next step — downloading the output — instead of staying on the
        compile action, which would only recompile and create another dated
        output folder."""

        html = self.pagina(compileResult={
            "message": "Listini compilati correttamente.",
            "outputs": [],
            "zipUrl": "/api/consegna/zip",
            "zipNome": "ordini.zip",
        })

        self.assertEqual(html.count("button--primary"), 1)
        primario = html.split("button--primary", 1)[1][:200]
        self.assertIn("Scarica i listini", primario)
        self.assertIn("Rifai i listini", html)
        self.assertNotIn("Compila i listini", html)

    def test_il_comando_finale_non_grida(self) -> None:
        html = self.pagina()

        self.assertIn("Compila i listini", html)
        self.assertNotIn("COMPILA I LISTINI", html)

    def test_lo_spostamento_resta_un_comando_secondario(self) -> None:
        """Being below the minimum-order threshold must not make this button
        primary: with several suppliers below threshold there would be
        several primary buttons competing with the real next step."""

        html = self.pagina()
        comando = html.split('data-action="move-supplier"', 1)[0]

        self.assertIn("button--secondary", comando[-260:])
        # The card stays highlighted: color conveys "below threshold", not a
        # sentence repeating the card's own heading.
        self.assertIn("supplier-summary__move is-below", html)
        # Inside the supplier card, "below minimum order" reads once, in the
        # heading. (The page-top warning is a separate place and a separate
        # concern.)
        scheda = html.split('<details class="supplier-summary"', 1)[1].split("</details>", 1)[0]
        self.assertEqual(scheda.count("al minimo d’ordine"), 1)

    def test_la_scheda_finale_non_ripete_le_promesse_gia_fatte(self) -> None:
        html = self.pagina()

        self.assertNotIn("Pronto per creare i listini?", html)
        self.assertNotIn("I documenti originali restano invariati", html)
        # The one promise no other line makes stays.
        self.assertIn("Nessun ordine viene inviato.", html)


class UnaConfermaGiaDataSiLeggeEeSiRevoca(BancoDiProva):
    """Confirmations, shown inside the row of the product they belong to.

    Confirmations are keyed by item — barcode plus name — not by row, which
    is what lets them survive next week's price list. Until the page said so,
    an already-checked box looked like leftover state to double-check; what
    was missing wasn't the underlying logic, just the two lines explaining
    since when it's valid and how to undo it. Exercised tests.
    """

    def revisione(self, prodotto: dict) -> dict:
        return {
            **REVISIONE,
            "products": [{
                "id": "p1",
                "name": "DIXOR POLVERE CLASSICO 40MISURINI",
                "ean": "8009408415265",
                "quantity": 3,
                "selectedSupplierId": "cipresso",
                "requiresConfirmation": True,
                "confirmationMessage": "Il codice a barre non coincide: confronta nome e formato.",
                "offers": [{
                    "supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True,
                    "description": "DIXOR CLASSICO 40 MIS.",
                }],
                **prodotto,
            }],
        }

    def blocco(self, prodotto: dict) -> str:
        return self.esegui(
            "return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, revisione=self.revisione(prodotto), passo=2),
        )

    GIA_DATA = {
        "confirmed": True,
        "confirmation": {"supplierId": "cipresso", "since": "2026-08-12T09:14:00+02:00",
                         "article": "8009408415265|DIXOR POLVERE CLASSICO 40MISURINI"},
    }

    def test_una_domanda_aperta_resta_la_domanda_di_prima(self) -> None:
        """Without an answer there's nothing to add: it asks, and says why."""

        html = self.blocco({"confirmed": False})

        self.assertIn("È lo stesso articolo?", html)
        self.assertIn("Il codice a barre non coincide", html)
        self.assertNotIn("Già confermato", html)
        self.assertNotIn("is-answered", html)

    def test_una_conferma_gia_data_dice_da_quando(self) -> None:
        html = self.blocco(self.GIA_DATA)

        self.assertIn("Già confermato da te il 12 agosto", html)
        self.assertIn("is-answered", html)

    def test_e_dice_perche_sopravvive_al_listino_della_settimana_dopo(self) -> None:
        """Applies to the item, not the row: this is the whole point of storing confirmations."""

        html = self.blocco(self.GIA_DATA)

        self.assertIn("Vale per l’articolo", html)
        self.assertIn("non per la riga del listino", html)

    def test_e_dice_come_si_annulla(self) -> None:
        """Undo already worked; nothing on screen explained it.

        Undo is a button, not unchecking a box: yes and no answer the same
        question and are given the same way. Once answered, the question
        doesn't reappear — what stays is the answer given and a way to change
        it, matching how a "no" behaves in the offer grid.
        """

        html = self.blocco(self.GIA_DATA)

        self.assertIn("Cambio idea", html)
        self.assertIn("Riapre la domanda", html)
        self.assertNotIn("revoca", html.lower())
        # And the checkbox is gone entirely: leaving one next to the button
        # would mean two ways to give the same answer.
        self.assertNotIn("data-product-confirm", html)
        self.assertNotIn("type=\"checkbox\"", html)
        # Once answered, it isn't asked again: the question is closed.
        self.assertNotIn("Sì, è lo stesso", html)
        self.assertNotIn("No, non è lo stesso", html)

    def test_tolta_la_spunta_la_domanda_torna_aperta(self) -> None:
        """The stored confirmation persists until saved: showing "already
        confirmed" above an unchecked box would contradict it."""

        html = self.blocco({**self.GIA_DATA, "confirmed": False})

        self.assertNotIn("Già confermato", html)
        self.assertIn("Il codice a barre non coincide", html)

    def test_senza_data_la_frase_regge_lo_stesso(self) -> None:
        senza = {**self.GIA_DATA, "confirmation": {**self.GIA_DATA["confirmation"], "since": ""}}
        html = self.blocco(senza)

        self.assertIn("Già confermato da te.", html)

    def test_dove_una_conferma_non_serve_non_si_disegna_niente(self) -> None:
        html = self.blocco({"requiresConfirmation": False, **self.GIA_DATA})

        self.assertEqual(html.strip(), "")

    def test_il_magazzino_che_non_si_apre_arriva_in_pagina_come_un_avviso(self) -> None:
        """CONFERME_NON_DISPONIBILI goes through the shared warnings panel,
        not a dedicated one: it's a comparison warning like any other."""

        html = self.esegui(
            "return renderSourceWarnings();",
            preparazione=self.con_confronto(pipeline=None, revisione={**REVISIONE, "warnings": [{
                "code": "CONFERME_NON_DISPONIBILI",
                "severity": "warning",
                "blocking": False,
                "title": "Le conferme già date non sono disponibili",
                "message": "Il file delle conferme non si legge più.",
            }]}),
        )

        self.assertIn("Le conferme già date non sono disponibili", html)


class IlNoDiceCheCosaSuccedeAQuestoProdotto(BancoDiProva):
    """The "not the same item" answer: the line under the button, and the panels after.

    A generic paragraph must not be shared across products: it can't be
    identical when the reassignment target differs per product, and pointing
    at "the panel above" is wrong the moment it's read, since that panel is
    created AFTER the "no" answer. The same rule must be stated only once —
    under the button — and say the thing that matters before clicking: where
    THIS product ends up.
    """

    def revisione(self, offerte: list[dict], *, rifiutate: list[str] = ()) -> dict:
        # A rejected row is NOT available: this is how the service reports it
        # (`available` false plus `rifiutata`), and it's the pair the grid uses
        # to distinguish "rejected" from "doesn't carry it".
        return {
            **REVISIONE,
            "suppliers": [
                {"id": "larice", "name": "LARICE", "minimumOrder": 0},
                {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
            ],
            "products": [{
                "id": "p1",
                "name": "SUPERMICIONE 100GR VASCHETTA ITALIA MANZO",
                "ean": "8009427397412",
                "quantity": 3,
                "selectedSupplierId": offerte[0]["supplierId"],
                "requiresConfirmation": True,
                "confirmationMessage": "Unico candidato da 100 g: confronta nome e formato.",
                "offers": [{
                    **offerta,
                    **({"available": False,
                        "rifiutata": {"since": "2026-08-22T09:00:00+02:00",
                                      "description": offerta.get("description", "")}}
                       if offerta["supplierId"] in rifiutate else {}),
                } for offerta in offerte],
            }],
        }

    OFFERTA_LARICE = {"supplierId": "larice", "supplierName": "LARICE", "price": 9.28,
                      "unitsPerOrderUnit": 32, "available": True,
                      "description": "SUPERMICIONE 100GR VASCH ITAL MANZO"}
    OFFERTA_BETULLA = {"supplierId": "betulla", "supplierName": "BETULLA", "price": 9.12,
                     "unitsPerOrderUnit": 32, "available": True,
                     "description": "MICIONE GATTO Patè Con Manzo 100 Grammi"}

    def blocco(self, offerte: list[dict]) -> str:
        return self.esegui(
            "return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, revisione=self.revisione(offerte), passo=2),
        )

    def griglia(self, offerte: list[dict], rifiutate: list[str]) -> str:
        return self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None, revisione=self.revisione(offerte, rifiutate=rifiutate), passo=2),
        )

    def test_con_un_altro_fornitore_dice_a_chi_passa_l_ordine_e_a_che_prezzo(self) -> None:
        """The page already knows who takes over: `normalizeReview`'s own rule,
        the cheapest per piece. Asking the user again would be redundant."""

        html = self.blocco([self.OFFERTA_LARICE, self.OFFERTA_BETULLA])

        self.assertIn("LARICE esce da questo prodotto e l’ordine passa a BETULLA", html)
        self.assertIn("0,29", html)

    def test_con_l_ultimo_fornitore_dice_che_il_prodotto_resta_da_reperire(self) -> None:
        html = self.blocco([self.OFFERTA_LARICE])

        self.assertIn("è l’ultimo fornitore rimasto", html)
        self.assertIn("Prodotti da reperire", html)
        self.assertIn("con la sua quantità", html)

    def test_non_promette_piu_un_riquadro_che_non_c_e_ancora(self) -> None:
        """The way back lives in the rejection panel, created afterward:
        announcing it beforehand sent the user looking upward at nothing."""

        html = self.blocco([self.OFFERTA_LARICE, self.OFFERTA_BETULLA])

        self.assertNotIn("qui sopra", html)
        self.assertNotIn("Si torna indietro", html)

    def test_la_frase_e_legata_al_pulsante_per_chi_non_la_vede(self) -> None:
        """Without `aria-describedby` a screen reader announces just the
        button: the consequence text stays disconnected, unread nearby text."""

        html = self.blocco([self.OFFERTA_LARICE])

        self.assertIn('aria-describedby="rifiuto-nota-p1"', html)
        self.assertIn('id="rifiuto-nota-p1"', html)

    def test_mentre_la_risposta_va_il_pulsante_dice_che_sta_lavorando(self) -> None:
        """The matching "reopen the question" button shows a "waiting" state;
        this one must too, for the whole round trip of a save, an answer, and
        a reloaded comparison — not just disable silently."""

        html = self.esegui(
            "state.matches.answering = 'p1:larice:rifiuto'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None, revisione=self.revisione([self.OFFERTA_LARICE]), passo=2),
        )

        self.assertIn("Attendere…", html)
        self.assertNotIn("No, non è lo stesso</button>", html)

    def test_una_risposta_su_un_altra_scheda_non_fa_attendere_questa(self) -> None:
        """Disabled, yes — one answer in flight at a time — but not showing
        "waiting": nothing is pending here."""

        html = self.esegui(
            "state.matches.answering = 'p9:betulla:rifiuto'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None, revisione=self.revisione([self.OFFERTA_LARICE]), passo=2),
        )

        self.assertIn("disabled", html)
        self.assertIn("No, non è lo stesso</button>", html)
        # True for both answers: disabled, but neither shows as pending.
        self.assertIn("Sì, è lo stesso</button>", html)
        self.assertNotIn("Attendere…", html)

    def test_quanto_dura_un_no_si_legge_una_volta_sola(self) -> None:
        """Two rejected suppliers share one validity-period sentence, not a
        duplicate copy inside each panel."""

        html = self.griglia([self.OFFERTA_LARICE, self.OFFERTA_BETULLA],
                            rifiutate=["larice", "betulla"])

        self.assertEqual(html.count("se la settimana prossima ne propone un'altra"), 1)
        self.assertEqual(html.count("Rifiutata da te"), 2)
        self.assertEqual(html.count("Cambio idea: è lo stesso"), 2)

    def test_senza_nessun_rifiuto_la_regola_non_si_scrive(self) -> None:
        html = self.griglia([self.OFFERTA_LARICE, self.OFFERTA_BETULLA], rifiutate=[])

        self.assertNotIn("Un no vale finché", html)

    def test_la_scheda_dice_quanti_fornitori_hai_escluso_tu(self) -> None:
        """Showing "0 suppliers carry it" on a product rejected by all of them would
        repeat the same misleading phrasing the offer panel already dropped."""

        html = self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None,
                revisione=self.revisione([self.OFFERTA_LARICE, self.OFFERTA_BETULLA],
                                         rifiutate=["larice", "betulla"]),
                passo=2),
        )

        self.assertIn("2 rifiutati da te", html)


class DoveIlFornitoreScriveLeSueOfferte(BancoDiProva):
    """Commercial conditions, declared from the page instead of hand-edited.

    A new supplier could be read and configured entirely from the page, but
    its promotional-offer layout could not: `commercial_conditions` had to be
    hand-edited into the adapter registry, and only one supplier had it.
    Exercised tests.
    """

    ANTEPRIMA = {
        "runId": "run-1",
        "suppliers": [{"id": "quercia", "name": "QUERCIA"}],
        "documents": [{
            "profileId": "prof-1",
            "fileName": "QUERCIA.xlsx",
            "format": "xlsx",
            "sheets": [{
                "name": "Foglio1", "maxRow": 80, "maxColumn": 20, "headerRows": [1],
                "rows": [{"row": 1, "values": ["CODICE", "DESCRIZIONE", "PREZZO"]}],
                "columns": [], "sectionBreaks": [],
            }],
            "suggestion": {"role": "supplier", "supplierId": "quercia", "sheet": "Foglio1",
                           "headerRow": 1, "dataStartRow": 2, "columns": {}},
        }],
    }

    def preparazione(self, extra: str = "") -> str:
        return (
            self.con_confronto(pipeline={"stato": "ERRORE", "fermata": {"code": "SCHEMA_SCONOSCIUTO"}, "fasi": []})
            + f"initializeSchemaMapping({json.dumps(self.ANTEPRIMA)});"
            + extra
        )

    def documento(self, extra: str = "") -> str:
        return self.esegui(
            "return renderSchemaDocument(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(extra),
        )

    SCELTA = 'state.schemaMapping.values["prof-1"].columns.promotion_text = 1;'

    def test_la_colonna_delle_offerte_si_sceglie_come_le_altre(self) -> None:
        html = self.documento()

        self.assertIn("Dove scrive le sue offerte", html)
        self.assertIn('data-schema-column="promotion_text"', html)
        # Inside "Other columns", which starts collapsed: adds nothing to the
        # page for suppliers with no offers to declare.
        self.assertIn("Altre colonne", html)

    def test_finche_non_c_e_la_colonna_non_si_chiede_altro(self) -> None:
        """A layout without a column isn't a declaration: it's an empty field."""

        html = self.documento()

        self.assertNotIn('data-schema-field="commercialLayout"', html)
        self.assertNotIn("Nome dell’articolo in omaggio", html)

    def test_scelta_la_colonna_si_chiede_come_sono_scritte(self) -> None:
        html = self.documento(self.SCELTA)

        self.assertIn('data-schema-field="commercialLayout"', html)
        self.assertIn("Ogni riga porta la sua offerta", html)
        self.assertIn("Un blocco di righe per ogni offerta", html)
        # The two columns only the "blocks" layout requires stay hidden.
        self.assertNotIn("Nome dell’articolo in omaggio", html)

    def test_a_blocchi_si_chiedono_le_colonne_che_quella_forma_pretende(self) -> None:
        """The service rejects the declaration without them: better to ask now."""

        html = self.documento(self.SCELTA + 'state.schemaMapping.values["prof-1"].commercialLayout = "blocchi";')

        self.assertIn("Nome dell’articolo in omaggio", html)
        self.assertIn('data-schema-column="reward_description"', html)
        self.assertIn('data-schema-column="discount"', html)

    def test_la_dichiarazione_arriva_al_servizio(self) -> None:
        """Without `commercialConditions` in the request body the service
        wouldn't even look at the chosen column, and the question would have
        no effect."""

        corpo = self.esegui(
            "return schemaMappingPayload();",
            preparazione=self.preparazione(
                self.SCELTA + 'state.schemaMapping.values["prof-1"].commercialLayout = "blocchi";'
            ),
        )
        mappatura = corpo["mappings"][0]

        self.assertEqual(mappatura["commercialConditions"], {"layout": "blocchi"})
        self.assertEqual(mappatura["columns"]["promotion_text"], 1)

    def test_il_predefinito_e_vuoto_e_vuol_dire_una_riga_per_offerta(self) -> None:
        corpo = self.esegui(
            "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )

        self.assertEqual(corpo["mappings"][0]["commercialConditions"], {"layout": ""})

    def test_su_ogni_listino_la_pagina_dice_se_le_offerte_le_legge_qualcuno(self) -> None:
        """`null` isn't an empty state to hide: it's a call to action."""

        base = (
            'state.colonneDocumenti = { caricate: true, caricando: false, runId: "run-1", motivo: "", errore: "", '
            'perNome: { "listino.xlsx": { fileName: "listino.xlsx", role: "supplier", sheet: "S", '
            'headerRow: 1, dataStartRow: 2, origin: "registro", columns: [], commercialConditions: %s } } };'
        )
        scheda = (
            'return renderFileCard({ id: "f1", name: "listino.xlsx", kind: "Listino", '
            'supplier: "QUERCIA", status: "ready", rows: 10, message: "" });'
        )

        nessuno = self.esegui(scheda, preparazione=self.con_confronto() + (base % "null"))
        self.assertIn("non le legge nessuno", nessuno)

        letto = self.esegui(scheda, preparazione=self.con_confronto() + (base % (
            '{ layout: "riga", sheet: "S", fields: { text: { campo: "promotion_text", colonna: 17, lettera: "Q" } } }'
        )))
        self.assertIn("Le offerte del fornitore si leggono nella colonna", letto)
        self.assertIn("<strong>Q</strong>", letto)
        self.assertNotIn("non le legge nessuno", letto)

    def test_del_gestionale_non_si_chiede_dove_scrive_le_offerte(self) -> None:
        """Not a supplier: the question has no possible answer."""

        html = self.esegui(
            'return renderFileCard({ id: "f1", name: "gestionale.xlsx", kind: "Gestionale", '
            'supplier: "Gestionale", status: "ready", rows: 451, message: "" });',
            preparazione=self.con_confronto()
            + 'state.colonneDocumenti = { caricate: true, caricando: false, runId: "run-1", motivo: "", errore: "", '
              'perNome: { "gestionale.xlsx": { fileName: "gestionale.xlsx", role: "master", sheet: "S", '
              'headerRow: 1, dataStartRow: 2, origin: "registro", columns: [], commercialConditions: null } } };',
        )

        self.assertNotIn("non le legge nessuno", html)


class LoScontoDelFornitoreSiRilegge(BancoDiProva):
    """A saved discount must survive a page reload instead of reading back as 0. Exercised tests.

    The service saved it and prices really did drop: only the input field was
    lying. `normalizeReview` builds a new object field by field, and
    `payload.state` — where discounts travel — wasn't among them, so
    `state.review.state` never existed and `renderStickySupplierTotals` always
    read `{}`.
    """

    def confronto_con_sconti(self, sconti: dict) -> str:
        revisione = {**REVISIONE, "state": {"supplierDiscounts": sconti}}
        return self.con_confronto(revisione=revisione, passo=2)

    def test_la_percentuale_salvata_torna_nel_campo(self) -> None:
        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti({"cipresso": 6}),
        )

        self.assertIn('data-supplier-discount="cipresso"', html)
        self.assertIn('value="6"', html)

    def test_i_decimali_non_si_perdono(self) -> None:
        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti({"cipresso": 7.5}),
        )

        self.assertIn('value="7.5"', html)

    def test_un_fornitore_senza_sconto_resta_col_campo_vuoto(self) -> None:
        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti({"cipresso": 6}),
        )

        campo_betulla = html.split('data-supplier-discount="betulla"')[0].split("<label")[-1]
        self.assertIn('value=""', campo_betulla)

    def test_senza_nessuno_sconto_non_si_inventa_un_numero(self) -> None:
        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti({}),
        )

        self.assertNotIn("has-discount", html)

    def test_quello_che_arriva_diventa_un_numero_o_non_passa(self) -> None:
        """On disk the discount is a `{rate, runId}` object: the service
        reduces it to a plain percentage, and this test guards that second
        line of defense. Without it, `escapeHtml` would write "[object
        Object]" into a numeric field — the same broken screen as before, just
        harder to diagnose."""

        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti(
                {"cipresso": {"rate": 0.06, "runId": "R1"}, "betulla": "8"},
            ),
        )

        self.assertNotIn("object Object", html)
        campo_cipresso = html.split('data-supplier-discount="cipresso"')[0].split("<label")[-1]
        self.assertIn('value=""', campo_cipresso)
        # A number written as text still reads as a number: the case of a
        # comparison saved by an older version.
        self.assertIn('value="8"', html)


class LaMerceCheNessunoHaNonFermaLaCompilazione(BancoDiProva):
    """A product the management software asks for that no price list carries. Exercised tests.

    The service accepts this case — the product surfaces in the "to be
    sourced" list — but the page was declaring it "Missing supplier ·
    blocking" and asking to zero out the quantity, i.e. to delete the number
    that's needed to go source it elsewhere.
    """

    def prodotto(self, **campi) -> dict:
        base = {
            "id": "p9",
            "name": "LENIS 87 LAVAGGI FRESCA FIORITURA",
            "quantity": 1,
            "selectedSupplierId": "",
            "offers": [{"supplierId": "cipresso", "available": False}],
            "warnings": [{
                "id": "p9-senza-offerta",
                "code": "SENZA_OFFERTA_UTILIZZABILE",
                "title": "Nessun fornitore ce l’ha",
                "message": "Nessuno dei 4 listini caricati ha una riga utilizzabile per questo prodotto.",
                "severity": "warning",
                "blocking": False,
                "productId": "p9",
            }],
        }
        return {**base, **campi}

    def avvisi(self, prodotto: dict, *, preparazione_extra: str = "") -> list:
        revisione = {**REVISIONE, "products": [prodotto]}
        return self.esegui(
            "return collectIssues().map((voce) => ({ title: voce.title, blocking: voce.blocking }));",
            preparazione=self.con_confronto(revisione=revisione, passo=3) + preparazione_extra,
        )

    def test_non_e_un_bloccante(self) -> None:
        voci = self.avvisi(self.prodotto())

        self.assertEqual([voce for voce in voci if voce["blocking"]], [])

    def test_ma_il_prodotto_lo_dice_lo_stesso(self) -> None:
        voci = self.avvisi(self.prodotto())

        self.assertIn("Nessun fornitore ce l’ha", [voce["title"] for voce in voci])

    def test_chi_invece_ha_un_offerta_e_non_l_ha_scelta_resta_bloccante(self) -> None:
        """The old blocking rule still applies when there's a real choice to make.

        Reached only by manually clearing the selection: `normalizeReview`
        always assigns the cheapest available offer, and supplier options only
        exist for available offers. This guards that invariant — the only
        state where "pick an available offer" has an action behind it.
        """

        voci = self.avvisi(
            self.prodotto(offers=[
                {"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True},
            ], warnings=[]),
            preparazione_extra='state.review.products[0].selectedSupplierId = "";',
        )

        self.assertIn("Fornitore mancante", [voce["title"] for voce in voci if voce["blocking"]])


class IlFiltroMostraSoloQuelloCheServe(BancoDiProva):
    """The status filter had overlapping, confusing categories. Exercised tests.

    "To check" caught any warning, including products already covered by
    "Nobody has it", and "No quantity" was just "To order" read backward.
    """

    ELENCO = [
        {"id": "p1", "name": "Da ordinare", "quantity": 3, "selectedSupplierId": "cipresso",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}]},
        {"id": "p2", "name": "Da confermare", "quantity": 2, "selectedSupplierId": "cipresso",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True,
                     "requiresConfirmation": True}]},
        # Carries its own warning, as on a real comparison: a generic
        # "to check" filter would also catch this, causing the same product
        # to appear under two different filter names.
        {"id": "p3", "name": "Nessuno ce l’ha", "quantity": 1, "selectedSupplierId": "",
         "offers": [{"supplierId": "cipresso", "available": False}],
         "warnings": [{"id": "p3-senza-offerta", "code": "SENZA_OFFERTA_UTILIZZABILE",
                       "title": "Nessun fornitore ce l’ha", "productId": "p3",
                       "message": "Nessun listino caricato porta questo prodotto.",
                       "severity": "warning", "blocking": False}]},
        {"id": "p4", "name": "Senza quantita", "quantity": 0, "selectedSupplierId": "",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}]},
        # Not "nobody has it": the price lists did have this row, and the
        # user removed it by answering "not the same item". Grouping it under
        # "Nobody has it" together with p3 would use a label that's false
        # for it.
        {"id": "p5", "name": "Scartato da me", "quantity": 4, "selectedSupplierId": "",
         "offers": [
             {"supplierId": "cipresso", "available": False,
              "rifiutata": {"since": "2026-08-22T10:00:00+00:00", "description": "SAPONE"}},
             {"supplierId": "betulla", "available": False,
              "rifiutata": {"since": "2026-08-22T10:01:00+00:00", "description": "SAPONE"}},
         ]},
    ]

    def barra(self, *, preparazione_extra: str = "") -> str:
        revisione = {**REVISIONE, "products": self.ELENCO}
        return self.esegui(
            "return renderToolbar();",
            preparazione=self.con_confronto(revisione=revisione, passo=2) + preparazione_extra,
        )

    def test_le_voci_sono_quelle_che_hanno_qualcosa_da_mostrare(self) -> None:
        html = self.barra()

        self.assertIn(">Tutti i prodotti (5)<", html)
        self.assertIn(">Da ordinare (4)<", html)
        self.assertIn(">Da confermare (1)<", html)
        self.assertIn(">Nessuno ce l’ha (1)<", html)
        self.assertIn(">Hai risposto no (1)<", html)
        # Nothing excluded and no pending order: two rows that would only be
        # read to discover they're empty.
        self.assertNotIn(">Esclusi (0)<", html)
        self.assertNotIn("Già ordinati", html)

    def test_le_voci_che_si_sovrapponevano_non_ci_sono_piu(self) -> None:
        html = self.barra()

        self.assertNotIn("Da verificare", html)
        self.assertNotIn("Senza quantità", html)

    def test_esclusi_compare_appena_c_e_un_escluso(self) -> None:
        html = self.barra(preparazione_extra='state.excludedProductIds.add("p4");')

        self.assertIn(">Esclusi (1)<", html)

    def test_gia_ordinati_compare_quando_c_e_un_ordine_in_sospeso(self) -> None:
        """The entry is derived from the data, not from a hardcoded list in the toolbar."""

        revisione = {**REVISIONE, "products": [
            *self.ELENCO,
            {"id": "p6", "name": "Ordinato la settimana scorsa", "quantity": 1,
             "selectedSupplierId": "cipresso",
             "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
             "pendingOrders": [{"orderId": "o1", "supplier": "cipresso", "supplierName": "CIPRESSO"}]},
        ]}
        html = self.esegui(
            "return renderToolbar();",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )
        nomi = self.esegui(
            'state.filters.status = "pending"; return filteredProducts().map((prodotto) => prodotto.name);',
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

        self.assertIn(">Già ordinati (1)<", html)
        self.assertEqual(nomi, ["Ordinato la settimana scorsa"])

    def test_la_voce_scelta_resta_anche_se_e_scesa_a_zero(self) -> None:
        """A dropdown that drops its selected option would misstate what's shown."""

        html = self.barra(preparazione_extra='state.filters.status = "pending";')

        self.assertIn(">Già ordinati (0)<", html)
        self.assertIn('value="pending" selected', html)

    def test_ogni_voce_mostra_i_prodotti_che_promette(self) -> None:
        revisione = {**REVISIONE, "products": self.ELENCO}
        nomi = self.esegui(
            "return Object.fromEntries(Object.keys(FILTRI_PRODOTTO).map((stato) => {"
            "  state.filters.status = stato;"
            "  return [stato, filteredProducts().map((prodotto) => prodotto.name)];"
            "}));",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

        self.assertEqual(
            nomi["ordered"],
            ["Da ordinare", "Da confermare", "Nessuno ce l’ha", "Scartato da me"],
        )
        # "Nobody has it" is NOT "to confirm": this overlap is what made the
        # old filter incomprehensible, with the same product listed under two
        # names promising two different things.
        self.assertEqual(nomi["to-confirm"], ["Da confermare"])
        # And it isn't "rejected by me" either. The price lists did have that
        # row; the user removed it. The two lists are disjoint because the
        # next move differs — one needs sourcing elsewhere, the other may just
        # need the answer revisited.
        self.assertEqual(nomi["missing"], ["Nessuno ce l’ha"])
        self.assertEqual(nomi["rejected"], ["Scartato da me"])
        self.assertEqual(nomi["excluded"], [])

    def test_il_tipo_di_prodotto_si_chiede_solo_se_ce_n_e_piu_d_uno(self) -> None:
        """On a real comparison, "Kits" is usually empty: a row to read past and discard."""

        solo_prodotti = self.barra()
        self.assertNotIn("Tipo di prodotto", solo_prodotti)

        revisione = {**REVISIONE, "products": [
            *self.ELENCO,
            {"id": "p5", "name": "Espositore", "itemType": "display", "quantity": 1,
             "selectedSupplierId": "cipresso",
             "offers": [{"supplierId": "cipresso", "price": 90, "declaredUnits": 12, "available": True}]},
        ]}
        con_espositori = self.esegui(
            "return renderToolbar();",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )
        self.assertIn("Tipo di prodotto", con_espositori)
        self.assertIn(">Espositori<", con_espositori)
        self.assertNotIn(">Kit<", con_espositori)


class LAvvisoPortaDoveSiRispondeTests(BancoDiProva):
    """Warnings link straight to the filter that shows the affected products. Exercised tests.

    A warning that only names the matching filter still leaves the user to
    get there by hand: switch page, open the dropdown, find the entry. It
    matters most on page 3, where a blocking warning says compilation can't
    start and must also say how to unblock it.
    """

    DA_CONFERMARE = {
        **REVISIONE,
        "warnings": [{
            "code": "RIFIUTI_CON_CANDIDATO_FORTE",
            "title": "Proposte da controllare",
            "message": "9 prodotti hanno una proposta di un fornitore da confermare o rifiutare.",
            "severity": "warning",
            "blocking": False,
        }],
    }

    def test_l_avviso_dei_prodotti_porta_il_comando(self) -> None:
        html = self.esegui(
            "return renderSourceWarnings(2);",
            preparazione=self.con_confronto(revisione=self.DA_CONFERMARE, passo=2),
        )

        self.assertIn('data-action="vai-al-filtro"', html)
        self.assertIn('data-filtro="to-confirm"', html)
        self.assertIn("Vai ai prodotti da confermare", html)

    def test_premerlo_applica_il_filtro_e_porta_in_pagina_due(self) -> None:
        """Checks the handler actually exists: without it, this would stay a
        button that does nothing while the markup looks identical."""

        esito = self.esegui(
            self.con_confronto(revisione=self.DA_CONFERMARE, passo=3) + """
              state.filters.search = "qualcosa";
              state.filters.type = "espositori";
              premi("vai-al-filtro", { filtro: "to-confirm" });
              return { passo: state.currentStep, stato: state.filters.status,
                       ricerca: state.filters.search, tipo: state.filters.type };
            """,
            risposte={"/api/history/pending": {"pending": []}},
        )

        self.assertEqual(esito["passo"], 2)
        self.assertEqual(esito["stato"], "to-confirm")
        # Search and type filters would narrow the list the warning promised.
        self.assertEqual(esito["ricerca"], "")
        self.assertEqual(esito["tipo"], "all")

    def test_un_filtro_inventato_non_lascia_la_pagina_su_un_elenco_vuoto(self) -> None:
        esito = self.esegui(
            self.con_confronto(revisione=self.DA_CONFERMARE, passo=3) + """
              premi("vai-al-filtro", { filtro: "non-esiste" });
              return { passo: state.currentStep, stato: state.filters.status };
            """,
        )

        self.assertEqual(esito["passo"], 3)
        self.assertEqual(esito["stato"], "all")

    def test_il_bloccante_della_pagina_tre_porta_alle_conferme(self) -> None:
        """The "Confirmation required · blocking" warning stops compilation, and now says
        how to unblock it."""

        revisione = {
            **REVISIONE,
            "products": [{
                "id": "p1",
                "name": "Prodotto uno",
                "quantity": 3,
                "selectedSupplierId": "cipresso",
                "requiresConfirmation": True,
                "confirmed": False,
                "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
            }],
        }

        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(revisione=revisione, passo=3),
        )

        bloccanti = html.split("<h3>Da risolvere prima di compilare</h3>", 1)[1].split("</section>", 1)[0]
        self.assertIn("Conferma richiesta · bloccante", bloccanti)
        self.assertIn('data-filtro="to-confirm"', bloccanti)
        self.assertIn("Vai alle conferme", bloccanti)

    def test_sulla_scheda_del_prodotto_il_comando_non_c_e(self) -> None:
        """There, the link would point to where the user already is, with the
        answer button right below.

        The supplier is cleared AFTER normalization: `normalizeReview` picks
        the cheapest offer on its own when none is selected, so a product
        that starts without a supplier doesn't stay that way.
        """

        html = self.esegui(
            self.con_confronto(passo=2) + """
              state.review.products[0].selectedSupplierId = "";
              invalidateIssues();
              return renderProductIssues(state.review.products[0]);
            """,
        )

        self.assertIn("Fornitore mancante", html)
        self.assertNotIn("vai-al-filtro", html)


class OgniAvvisoStaDovePuoiFarciQualcosaTests(BancoDiProva):
    """A generic "know before you continue" panel repeated verbatim on three pages.

    Repetition loses weight: seeing the same panel three times trains the
    user to stop reading it. Documents now surface where documents are
    managed, products where they're answered, and each page has its own title.
    """

    MISTI = {
        **REVISIONE,
        "warnings": [
            {
                "code": "ANOMALIE_LISTINO",
                "title": "LARICE: su 3 righe il prezzo può essere più alto del vero",
                "message": "3 righe del listino LARICE hanno una sigla che il programma non conosce.",
                "severity": "warning",
                "blocking": False,
            },
            {
                "code": "PRODOTTI_SENZA_OFFERTA",
                "title": "Prodotti che nessun fornitore ha",
                "message": "34 prodotti dell'elenco non li ha nessuno dei fornitori.",
                "severity": "warning",
                "blocking": False,
            },
        ],
    }

    def riquadro(self, pagina: int) -> str:
        return self.esegui(
            f"return renderSourceWarnings({pagina});",
            preparazione=self.con_confronto(revisione=self.MISTI, passo=pagina),
        )

    def test_la_pagina_uno_parla_dei_documenti(self) -> None:
        html = self.riquadro(1)

        self.assertIn("Da sapere sui documenti caricati", html)
        self.assertIn("LARICE", html)
        self.assertNotIn("Prodotti che nessun fornitore ha", html)

    def test_la_pagina_due_parla_dei_prodotti(self) -> None:
        html = self.riquadro(2)

        self.assertIn("Da sapere sui prodotti", html)
        self.assertIn("Prodotti che nessun fornitore ha", html)
        self.assertNotIn("LARICE", html)

    def test_i_due_titoli_non_sono_lo_stesso(self) -> None:
        self.assertNotIn("Da sapere prima di continuare", self.riquadro(1) + self.riquadro(2))

    def test_un_bloccante_resta_su_tutt_e_due_le_pagine(self) -> None:
        """Blocks compilation: not hidden regardless of ordering."""

        revisione = {
            **REVISIONE,
            "warnings": [{
                "code": "CATALOGO_FORNITORE_NON_LETTO",
                "title": "Un listino non è stato letto",
                "message": "Il confronto è senza BETULLA.",
                "severity": "error",
                "blocking": True,
            }],
        }
        pagine = [
            self.esegui(
                f"return renderSourceWarnings({pagina});",
                preparazione=self.con_confronto(revisione=revisione, passo=pagina),
            )
            for pagina in (1, 2)
        ]

        for html in pagine:
            self.assertIn("Un listino non è stato letto", html)

    def test_un_codice_mai_visto_finisce_dove_si_vede_sempre(self) -> None:
        """Page 1 opens first: an unrecognized warning code must not silently
        disappear there."""

        revisione = {
            **REVISIONE,
            "warnings": [{
                "code": "COSA_NUOVA_MAI_VISTA",
                "title": "Qualcosa di nuovo",
                "message": "Un avviso che questo elenco non conosce.",
                "severity": "warning",
                "blocking": False,
            }],
        }
        uno, due = (
            self.esegui(
                f"return renderSourceWarnings({pagina});",
                preparazione=self.con_confronto(revisione=revisione, passo=pagina),
            )
            for pagina in (1, 2)
        )

        self.assertIn("Qualcosa di nuovo", uno)
        self.assertEqual(due, "")


class LaPaginaTreNonRipeteGliAvvisiDiProdotto(BancoDiProva):
    """Page 3 avoids printing dozens of individual per-product warnings. Exercised tests.

    Per-product warnings are already shown on each product's own card on page
    2, where the answer button lives; page 3 instead shows a two-line summary
    that counts them and names the filter that finds them.
    """

    def pagina(self) -> str:
        revisione = {
            **REVISIONE,
            "warnings": [{
                "code": "PRODOTTI_SENZA_OFFERTA",
                "title": "Prodotti che nessun fornitore ha",
                "message": "34 prodotti dell'elenco non hanno nessuna offerta utilizzabile.",
                "severity": "warning",
                "blocking": False,
            }],
            "products": [{
                "id": f"p{indice}",
                "name": f"Prodotto {indice}",
                "quantity": 1,
                "selectedSupplierId": "cipresso",
                "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
                "warnings": [{
                    "id": f"p{indice}-rifiuto",
                    "code": "RIFIUTO_CON_CANDIDATO_FORTE",
                    "title": "Possibile prodotto CIPRESSO",
                    "message": f"CIPRESSO propone «PRODOTTO {indice}». Indica se è lo stesso prodotto.",
                    "severity": "warning",
                    "blocking": False,
                    "productId": f"p{indice}",
                }],
            } for indice in range(1, 40)],
        }
        return self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(revisione=revisione, passo=3),
        )

    def test_il_riquadro_conta_gli_avvisi_dell_ordine_non_quelli_delle_righe(self) -> None:
        html = self.pagina()

        # The two that concern the order as a whole: the no-offer summary and
        # the unmet minimum-order threshold. The per-product ones don't count.
        self.assertIn("2 avvisi", html)
        self.assertNotIn("41 avvisi", html)

    def test_il_riepilogo_che_li_conta_tutti_resta(self) -> None:
        html = self.pagina()

        self.assertIn("Prodotti che nessun fornitore ha", html)

    def test_e_i_trentanove_non_si_stampano_uno_per_uno(self) -> None:
        html = self.pagina()

        self.assertNotIn("Possibile prodotto CIPRESSO", html)


class ComeIlFornitoreChiamaQuelCheVende(BancoDiProva):
    """A matching EAN doesn't guarantee a matching color. Exercised tests.

    For colored items, an exact EAN match can still hide a color mismatch
    between suppliers. Prices alone don't distinguish them: the supplier's
    own product description — the only place the color is written — is sent
    by the service and must not be dropped by the page.
    """

    def griglia(self, offerte: list) -> str:
        prodotto = {
            "id": "p1",
            "name": "DOPLO PIATTI PIANI 20PZ",
            "ean": "8059402029357",
            "quantity": 1,
            "selectedSupplierId": "betulla",
            "offers": offerte,
        }
        revisione = {**REVISIONE, "products": [prodotto]}
        return self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    def test_il_nome_del_fornitore_si_legge_anche_con_l_ean_uguale(self) -> None:
        html = self.griglia([{
            "supplierId": "betulla", "price": 8.50, "unitsPerOrderUnit": 10, "available": True,
            "description": "DOPLO Piatto Riutilizzabile Piano In Polipropilene Bianco Di",
            "ean": "8059402029357", "supplierCode": "1310362",
        }])

        self.assertIn("Polipropilene Bianco", html)
        self.assertIn("cod. 1310362", html)

    def test_l_ean_uguale_non_si_ripete(self) -> None:
        """Already shown once at the top of the card: repeating it per row is noise."""

        html = self.griglia([{
            "supplierId": "betulla", "price": 8.50, "unitsPerOrderUnit": 10, "available": True,
            "description": "DOPLO Piatto Bianco", "ean": "8059402029357",
        }])

        self.assertNotIn("EAN 8059402029357", html)

    def test_un_ean_diverso_si_dice(self) -> None:
        """A mismatched EAN means the row was matched some other way: the
        first thing to check, not the last."""

        html = self.griglia([{
            "supplierId": "betulla", "price": 8.50, "unitsPerOrderUnit": 10, "available": True,
            "description": "LUXA SAPONE EROGATORE ORIGINAL ML.250", "ean": "8729721830575",
        }])

        self.assertIn("EAN 8729721830575", html)

    def test_un_fornitore_che_non_dice_niente_non_aggiunge_una_riga_vuota(self) -> None:
        html = self.griglia([{
            "supplierId": "betulla", "price": 8.50, "unitsPerOrderUnit": 10, "available": True,
        }])

        self.assertNotIn("offer-card__riga-fornitore", html)


class IlVisualizzatoreDeiListini(BancoDiProva):
    """Browsing a supplier's price list and matching an item by hand.

    Fixture case: `LUXA SAPONE LIQ. EROG.250ML` is in the management-software
    export under 4009428623194, used only by CIPRESSO; NOCE lists the same
    item under 8729721830575 at 1.15 instead of 1.28. The text alone can't
    tell them apart (the `ORIGINAL` variant isn't in the export's name), so
    the price list itself has to be checked.
    """

    PRODOTTO = {
        "id": "p1",
        "name": "LUXA SAPONE LIQ. EROG.250ML",
        "ean": "4009428623194",
        "quantity": 1,
        "selectedSupplierId": "cipresso",
        "offers": [{
            "supplierId": "cipresso", "price": 7.68, "unitsPerOrderUnit": 6, "available": True,
            "description": "LUXA SAPONE LIQUIDO EROGATORE 250", "sourceRow": 900,
        }],
    }
    RIGHE = [
        {"sourceRow": 4793, "ean": "8729014462339", "description": "LUXA SAPONE EROGATORE GO FRESH ML.250",
         "piecesPerCarton": 6, "unitPriceNet": 1.15, "ordinabile": True, "supplierCode": "0000429062"},
        {"sourceRow": 4794, "ean": "8729721830575", "description": "LUXA SAPONE EROGATORE ORIGINAL ML.250",
         "piecesPerCarton": 6, "unitPriceNet": 1.15, "ordinabile": True},
        {"sourceRow": 5000, "ean": "", "description": "RIGA SEPARATORE",
         "piecesPerCarton": None, "unitPriceNet": None, "ordinabile": False, "motivo": "senza prezzo"},
    ]

    def finestra(self, *, listino: dict | None = None, uguaglianze: list | None = None) -> str:
        revisione = {
            **REVISIONE,
            "products": [self.PRODOTTO],
            "uguaglianze": uguaglianze if uguaglianze is not None else [],
        }
        apparecchiato = {
            "aperto": True, "prodottoId": "p1", "fornitore": "noce",
            "fornitori": [
                {"id": "noce", "name": "NOCE", "righe": 8881, "ordinabili": 8869},
                {"id": "betulla", "name": "BETULLA", "righe": 6379, "ordinabili": 6379},
            ],
            "righe": self.RIGHE, "totale": 8881, "trovate": 3, "scartate": 12,
            "rigaFuoco": "4794", "da": 0, "quante": 50, "query": "",
            **(listino or {}),
        }
        return self.esegui(
            "return renderListinoDialog();",
            preparazione=self.con_confronto(revisione=revisione, passo=2)
            + f"state.listino = {{ ...state.listino, ...{json.dumps(apparecchiato)} }};",
        )

    def test_il_listino_si_legge_come_il_programma_l_ha_letto(self) -> None:
        html = self.finestra()

        self.assertIn("Listino NOCE", html)
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL ML.250", html)
        self.assertIn("8729721830575", html)
        self.assertIn("cod. 0000429062", html)

    def test_le_righe_scartate_si_contano_sempre(self) -> None:
        """A list showing 8869 of 8881 rows without saying so hides data.

        "8881" not "8.881": Italian groups thousands from five digits up
        (`minimumGroupingDigits` 2), which `Intl` handles. Writing the dot
        here would make the test pass only under a different locale.
        """

        html = self.finestra()

        self.assertIn("8881 righe", html)
        self.assertIn("12 non ordinabili", html)

    def test_una_riga_non_ordinabile_si_vede_e_dice_perche_ma_non_si_abbina(self) -> None:
        html = self.finestra()

        self.assertIn("Non ordinabile: senza prezzo", html)
        self.assertNotIn('data-riga="5000"', html)

    def test_la_riga_su_cui_si_e_aperto_e_evidenziata(self) -> None:
        """With eight thousand rows, searching by hand for something the
        program already knows defeats the point of the tool."""

        html = self.finestra()

        self.assertIn("is-fuoco", html)

    def test_la_riga_gia_abbinata_non_si_riabbina(self) -> None:
        righe = [{**self.RIGHE[1], "sourceRow": 900}]
        html = self.finestra(listino={"righe": righe, "fornitore": "cipresso", "trovate": 1})

        self.assertIn("Già abbinata", html)
        self.assertNotIn('data-action="abbina-riga"', html)

    def test_il_conto_cambia_quando_si_cerca(self) -> None:
        html = self.finestra(listino={"query": "DOVE", "trovate": 3})

        self.assertIn("3 righe trovate su 8881", html)

    def test_le_uguaglianze_non_stanno_piu_qui(self) -> None:
        """Equality declarations live in Settings, not inside a product's
        own review data, and their display names the items instead of
        showing two bare thirteen-digit codes."""

        html = self.finestra(uguaglianze=[{
            "codici": ["4009428623194", "8729721830575"],
            "motivo": "scelta a mano dal listino NOCE, riga 4794",
        }])

        self.assertNotIn("Non sono lo stesso", html)
        self.assertNotIn("4009428623194 = 8729721830575", html)

    def test_da_ogni_prodotto_si_puo_aprire_il_listino(self) -> None:
        """Opens on the supplier that already has a matched row for this
        product; also the way to check it's really the same item, since a
        matching EAN alone doesn't guarantee that."""

        revisione = {**REVISIONE, "products": [self.PRODOTTO]}
        html = self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

        self.assertIn('data-action="apri-listino"', html)
        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga="900"', html)


class SfogliaApreIlFornitoreScelto(BancoDiProva):
    """"Sfoglia i listini" opens the price list of the *chosen* supplier, not
    the first supplier in `build_review_data.supplier_ids()`'s fixed order
    (betulla, larice, noce, cipresso) that happens to have a matched row.
    """

    ORDINE = ("betulla", "larice", "noce", "cipresso")

    def prodotto(self, scelto: str, righe: dict[str, int | None]) -> dict:
        """An offer per supplier, in the fixed order of the real list."""

        return {
            "id": "p1",
            "name": "LUXA SAPONE LIQ. EROG.250ML",
            "ean": "4009428623194",
            "quantity": 1,
            "selectedSupplierId": scelto,
            "offers": [
                {
                    "supplierId": fornitore,
                    "price": 7.68,
                    "unitsPerOrderUnit": 6,
                    "available": True,
                    "description": "LUXA SAPONE LIQUIDO EROGATORE 250",
                    "sourceRow": righe[fornitore],
                }
                for fornitore in self.ORDINE
                if fornitore in righe
            ],
        }

    def sfoglia(self, prodotto: dict) -> str:
        revisione = {**REVISIONE, "products": [prodotto]}
        return self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    def test_il_fornitore_scelto_vince_su_chi_viene_prima_nell_elenco(self) -> None:
        """Chosen NOCE, but BETULLA comes first in the list and has a row."""

        html = self.sfoglia(self.prodotto("noce", {"betulla": 1751, "noce": 4755}))

        self.assertIn('data-supplier-id="noce"', html)
        self.assertIn('data-riga="4755"', html)

    def test_il_fornitore_scelto_vince_anche_con_tre_righe_prima(self) -> None:
        """Chosen CIPRESSO, which is last in the fixed list order."""

        html = self.sfoglia(
            self.prodotto("cipresso", {"betulla": 1751, "larice": 6277, "cipresso": 900})
        )

        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga="900"', html)

    def test_quando_lo_scelto_e_l_unico_con_una_riga_non_cambia_niente(self) -> None:
        html = self.sfoglia(self.prodotto("noce", {"noce": 4755}))

        self.assertIn('data-supplier-id="noce"', html)
        self.assertIn('data-riga="4755"', html)

    def test_chi_non_ha_una_riga_abbinata_non_ruba_il_posto(self) -> None:
        """Chosen LARICE; BETULLA comes first but has no matched row."""

        html = self.sfoglia(self.prodotto("larice", {"betulla": None, "larice": 6277}))

        self.assertIn('data-supplier-id="larice"', html)
        self.assertIn('data-riga="6277"', html)

    def test_se_nessuno_ha_una_riga_si_apre_lo_stesso_quello_scelto(self) -> None:
        """No row to highlight, but the price list to open is still the
        chosen supplier's, not the first in the list."""

        html = self.sfoglia(self.prodotto("cipresso", {"betulla": None, "cipresso": None}))

        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga=""', html)


class IlComandoDeiListiniNonPromettteUnFornitoreSolo(BancoDiProva):
    """The button reads "Sfoglia i listini" (browse the price lists), plural
    and without naming a supplier: the dialog it opens lets the user switch
    between all suppliers without closing it, so naming just the first one
    on the label would undersell what the command does. Which price list
    opens first is still the chosen supplier, covered above.
    """

    def sfoglia(self, scelto: str) -> str:
        revisione = {**REVISIONE, "products": [{
            "id": "p1",
            "name": "LUXA SAPONE LIQ. EROG.250ML",
            "quantity": 1,
            "selectedSupplierId": scelto,
            "offers": [
                {"supplierId": "betulla", "price": 7.68, "unitsPerOrderUnit": 6,
                 "available": True, "sourceRow": 1751},
                {"supplierId": "cipresso", "price": 7.70, "unitsPerOrderUnit": 6,
                 "available": True, "sourceRow": 900},
            ],
        }]}
        return self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    def test_l_etichetta_non_cambia_col_fornitore_scelto(self) -> None:
        for scelto in ("betulla", "cipresso"):
            with self.subTest(scelto=scelto):
                html = self.sfoglia(scelto)

                self.assertIn(">Sfoglia i listini</button>", html)
                self.assertNotIn("Sfoglia il listino", html)

    def test_il_listino_che_si_apre_per_primo_resta_quello_scelto(self) -> None:
        """The label is generic, the choice isn't: two different things."""

        html = self.sfoglia("cipresso")

        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga="900"', html)


class UnaPropostaCheNonSiPuoAccettareLoDice(BancoDiProva):
    """A proposed candidate row carries only four columns from the
    shortlist; without pieces-per-carton the accept endpoint rejects it, so
    the UI must say up front why "Yes" won't go through.
    """

    def scheda(self, candidato: dict) -> str:
        prodotto = {
            "id": "p1",
            "name": "AURAPUR ELETTRICO RICARICA FIORI ELEGANTI",
            "quantity": 1,
            "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
            "warnings": [{
                "id": "p1-rifiuto-noce",
                "code": "RIFIUTO_CON_CANDIDATO_FORTE",
                "title": "Possibile prodotto NOCE",
                "message": "NOCE propone «AURAPUR ELETTRICO RICARICA FIORI».",
                "severity": "warning",
                "blocking": False,
                "productId": "p1",
                "supplierId": "noce",
                "candidateKey": "abc123",
                "candidate": candidato,
            }],
        }
        revisione = {**REVISIONE, "products": [prodotto]}
        return self.esegui(
            "return renderProductIssues(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    COMPLETO = {
        "supplierId": "noce",
        "supplierName": "NOCE",
        "candidateKey": "abc123",
        "description": "AURAPUR ELETTRICO RICARICA FIORI",
        "ean": "5419373044415",
        "unitPriceNet": 2.23,
        "quantityFactor": 9,
        "orderUnitPriceNet": 20.07,
        "available": True,
    }

    def test_una_proposta_completa_si_puo_accettare(self) -> None:
        html = self.scheda(self.COMPLETO)

        self.assertIn('data-accepted="true"', html)
        self.assertIn("9 pezzi per collo", html)

    def test_senza_i_pezzi_per_collo_il_si_non_compare(self) -> None:
        html = self.scheda({**self.COMPLETO, "quantityFactor": None, "orderUnitPriceNet": None, "available": False})

        self.assertNotIn('data-accepted="true"', html)
        self.assertIn('data-accepted="false"', html)

    def test_e_si_dice_che_cosa_manca(self) -> None:
        html = self.scheda({**self.COMPLETO, "quantityFactor": None, "orderUnitPriceNet": None, "available": False})

        self.assertIn("quanti pezzi ci sono in un collo", html)

    def test_e_non_si_inventa_un_pezzo_per_collo(self) -> None:
        """`Math.max(1, …)` must not print "1 piece per carton" for a missing value."""

        html = self.scheda({**self.COMPLETO, "quantityFactor": None, "orderUnitPriceNet": None, "available": False})

        self.assertNotIn("1 pezzo per collo", html)


class LaRigaPropostaPortaIlMotivoDelloScarto(BancoDiProva):
    """Two related defects on the same product card, both from the card
    knowing only the candidate row's price, not why it was rejected.

    First: the "is this the same item?" question showed code, EAN and price
    but not the reason the automatic match rejected the row (e.g. "it's a
    2-piece pack, the searched item is single") — the reason was in the data
    but `normalizeCandidate` dropped it, leaving only a similarity score
    behind "Why does this show up?", which means nothing to whoever orders.

    Second: the card could show "LARICE: not in the current price list"
    right below LARICE's own row with EAN, code and price — contradicting
    itself about the same supplier.
    """

    CANDIDATO = {
        "supplierId": "larice",
        "supplierName": "LARICE",
        "candidateKey": "7e48a3ad165ab9a51b96",
        "description": "LESTOX LEGNO PULITO 5IN1 2PZ 750ML",
        "ean": "5009421148617",
        "supplierCode": "7230",
        "rationale": "Il candidato \u00e8 una confezione da 2 pezzi, mentre l\u2019articolo cercato \u00e8 singolo.",
        "unitPriceNet": 3.8,
        "quantityFactor": 6,
        "orderUnitPriceNet": 22.8,
        "price": 22.8,
        "available": True,
    }

    def scheda(self, *, decisione: str = "", terzo_fornitore: bool = False) -> str:
        """The full card: the question sits above the table, the sentence below."""

        larice = {
            "supplierId": "larice",
            "available": False,
            "rejectedCandidate": self.CANDIDATO,
        }
        if decisione:
            larice["candidateDecision"] = decisione
        offerte = [
            {"supplierId": "betulla", "price": 33.84, "unitsPerOrderUnit": 12, "available": True},
            larice,
        ]
        if terzo_fornitore:
            offerte.append({"supplierId": "noce", "available": False})
        # The service clears the warning once a decision is recorded
        # (`apply_match_overrides`); the sentence below must not reappear either.
        avvisi = [] if decisione else [{
            "id": "p1-rifiuto-larice",
            "code": "RIFIUTO_CON_CANDIDATO_FORTE",
            "title": "Possibile prodotto LARICE",
            "message": "LARICE propone \u00abLESTOX LEGNO PULITO 5IN1 2PZ 750ML\u00bb. Indica se \u00e8 lo stesso articolo.",
            "technicalMessage": "Il candidato migliore \u00e8 stato rifiutato con somiglianza 0.90 su 1.",
            "severity": "warning",
            "blocking": False,
            "productId": "p1",
            "supplierId": "larice",
            "candidateKey": self.CANDIDATO["candidateKey"],
            "candidate": self.CANDIDATO,
        }]
        revisione = {
            **REVISIONE,
            "suppliers": [
                {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
                {"id": "larice", "name": "LARICE", "minimumOrder": 0},
                {"id": "noce", "name": "NOCE", "minimumOrder": 0},
            ],
            "products": [{
                "id": "p1",
                "name": "LESTOX LEGNO PULITO 5IN1 750ML",
                "ean": "5009552632702",
                "quantity": 1,
                "selectedSupplierId": "betulla",
                "offers": offerte,
                "warnings": avvisi,
            }],
        }
        return self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    def test_il_motivo_dello_scarto_si_legge_senza_aprire_niente(self) -> None:
        html = self.scheda()

        self.assertIn("L\u2019analisi automatica l\u2019ha scartata", html)
        self.assertIn("confezione da 2 pezzi", html)

    def test_la_domanda_usa_la_parola_dell_altra_domanda(self) -> None:
        """Two panels ask the same question and must use the same wording."""

        html = self.scheda()

        self.assertIn("\u00c8 lo stesso articolo?", html)
        self.assertNotIn("\u00c8 lo stesso prodotto?", html)

    def test_i_pulsanti_dicono_a_che_cosa_rispondono(self) -> None:
        html = self.scheda()

        self.assertIn("S\u00ec, \u00e8 lo stesso", html)
        self.assertIn("No, non \u00e8 lo stesso", html)

    def test_chi_propone_una_riga_non_e_uno_che_non_ce_l_ha(self) -> None:
        html = self.scheda()

        self.assertIn("LESTOX LEGNO PULITO 5IN1 2PZ 750ML", html)
        self.assertNotIn("non ce l\u2019hanno nel listino di adesso", html)

    def test_e_nemmeno_dopo_che_hai_risposto_no(self) -> None:
        """The recorded-answer panel already says "not the same item"; the
        sentence below can't also say "they don't have it"."""

        html = self.scheda(decisione="rejected")

        self.assertIn("NON \u00e8 lo stesso articolo", html)
        self.assertNotIn("non ce l\u2019hanno nel listino di adesso", html)

    def test_ma_un_fornitore_senza_riga_proposta_resta_nella_frase(self) -> None:
        """The sentence itself stays; only the contradicting supplier drops out."""

        html = self.scheda(terzo_fornitore=True)

        self.assertIn("NOCE: non ce l\u2019hanno nel listino di adesso.", html)
        self.assertNotIn("LARICE e NOCE", html)


class LeUguaglianzeStannoInImpostazioni(BancoDiProva):
    """The list of "these two codes are the same item" declarations, in
    Settings. It shows the item's name from the management-software export
    and from the price list, not just the two bare codes: judging whether a
    declaration is correct needs the names, not the numbers alone.
    """

    VOCE = {
        "codici": ["4009428623194", "8729721830575"],
        "gestionale": {"codice": "4009428623194", "nome": "LUXA SAPONE LIQ EROG 250ML"},
        "listino": {
            "fornitoreId": "noce",
            "fornitore": "NOCE",
            "codice": "8729721830575",
            "codiceFornitore": "0000000001205",
            "nome": "LUXA SAPONE EROGATORE ORIGINAL ML 250",
        },
        "motivo": "scelta a mano dal listino NOCE, riga 4794",
        "dal": "2026-08-17T17:59:39.470017+00:00",
    }

    def pannello(self, *, voci: list | None = None, query: str = "", totale: int | None = None,
                 caricando: bool = False, errore: str = "") -> str:
        elenco = [self.VOCE] if voci is None else voci
        return self.esegui(
            "return renderSettingsUguaglianzePanel();",
            preparazione=f"""
              state.impostazioni.uguaglianze = {{
                caricate: true, caricando: {json.dumps(caricando)},
                query: {json.dumps(query)},
                voci: {json.dumps(elenco)},
                totale: {json.dumps(len(elenco) if totale is None else totale)},
                motivo: "", errore: {json.dumps(errore)}, togliendo: "", timer: null,
              }};
            """,
        )

    def test_si_vedono_i_due_nomi_e_il_fornitore(self) -> None:
        html = self.pannello()

        self.assertIn("LUXA SAPONE LIQ EROG 250ML", html)
        self.assertIn("LUXA SAPONE EROGATORE ORIGINAL ML 250", html)
        self.assertIn("NOCE", html)
        self.assertIn("Gestionale", html)

    def test_si_vedono_anche_i_due_codici(self) -> None:
        """Names say whether it's the same item; codes say which declaration
        is being removed. Both are needed."""

        html = self.pannello()

        self.assertIn("4009428623194", html)
        self.assertIn("8729721830575", html)
        self.assertIn("cod. 0000000001205", html)

    def test_si_toglie_dalla_stessa_riga(self) -> None:
        html = self.pannello()

        self.assertIn('data-action="togli-uguaglianza"', html)
        self.assertIn('data-codici="4009428623194,8729721830575"', html)

    def test_c_e_un_campo_di_ricerca(self) -> None:
        html = self.pannello()

        self.assertIn("data-uguaglianze-cerca", html)

    def test_senza_dichiarazioni_lo_dice_e_dice_da_dove_nascono(self) -> None:
        html = self.pannello(voci=[])

        self.assertIn("Nessuna dichiarazione", html)
        # The link back to where declarations come from.
        self.assertIn("Sfoglia i listini", html)

    def test_una_ricerca_senza_esiti_non_dice_che_non_ce_ne_sono(self) -> None:
        """"No declarations" after typing a search term would be false: the
        declarations still exist, the search just found none of them."""

        html = self.pannello(voci=[], query="marsiglia", totale=7)

        self.assertIn("Nessuna dichiarazione trovata", html)
        self.assertIn("7 dichiarate in tutto", html)


class LeConfermeSiScaricanoDaImpostazioni(BancoDiProva):
    """`conferme.db` is the program's permanent memory and has no reader UI.

    A single confirmation shows on the row it applies to, with its date and
    item; there was no overview and no way to take a copy off that computer.
    """

    def test_c_e_un_collegamento_che_scarica_le_conferme(self) -> None:
        html = self.esegui("return renderSettingsConfermePanel();")

        self.assertIn("/api/conferme/esporta", html)
        # An `<a download>`, not a button calling the service: the response
        # is a file to save, and the page has nothing further to do with it.
        self.assertIn("download", html)
        self.assertIn("Scarica tutto quello che hai confermato", html)

    def test_dice_che_cosa_si_perde_se_si_perde_quel_file(self) -> None:
        """A link that just says "download" explains nothing: this file is
        the only way to read all confirmations together and the only copy
        that can leave this computer."""

        html = self.esegui("return renderSettingsConfermePanel();")

        self.assertIn("per sempre", html)
        self.assertIn("il file qui sotto è la tua copia", html)

    def test_il_pannello_e_dentro_la_pagina_delle_impostazioni(self) -> None:
        """A function nothing calls isn't a feature, it's dead code."""

        html = self.esegui(
            "return renderSettingsPage();",
            preparazione="state.impostazioni.aperta = true; state.impostazioni.caricate = true;",
        )

        self.assertIn("/api/conferme/esporta", html)
        self.assertIn("Le conferme che hai dato", html)


class LaColonnaDellOrdineSiCambia(BancoDiProva):
    """The order-quantity column can be changed from the document's card on
    page 1, not only once, up front, through the guided column mapping that
    only opens for a document whose layout the program doesn't recognize.
    """

    COLONNE = [
        {"colonna": 1, "lettera": "A", "intestazione": "EAN", "esempio": "8000000000001",
         "valori": 6380, "formule": 0, "occupataDa": "Codice a barre", "scegliibile": False, "attuale": False},
        {"colonna": 3, "lettera": "C", "intestazione": "ORDINE", "esempio": "",
         "valori": 1, "formule": 0, "occupataDa": "", "scegliibile": True, "attuale": True},
        {"colonna": 7, "lettera": "G", "intestazione": "Pedana", "esempio": "64",
         "valori": 6377, "formule": 0, "occupataDa": "", "scegliibile": True, "attuale": False},
        {"colonna": 9, "lettera": "I", "intestazione": "TOTALI", "esempio": "=C2*E2*F2",
         "valori": 6381, "formule": 6380, "occupataDa": "", "scegliibile": True, "attuale": False},
        {"colonna": 10, "lettera": "J", "intestazione": "", "esempio": "",
         "valori": 0, "formule": 0, "occupataDa": "", "scegliibile": True, "attuale": False},
    ]

    def finestra(self, scelta: int, *, errore: str = "", salvando: bool = False) -> str:
        return self.esegui(
            "return renderColonnaOrdineDialog();",
            preparazione=f"""
              state.colonnaOrdine = {{
                aperta: true, fornitore: "betulla", nome: "BETULLA",
                fileName: "listino.xlsx", foglio: "Sheet1",
                caricando: false, salvando: {json.dumps(salvando)},
                errore: {json.dumps(errore)},
                colonne: {json.dumps(self.COLONNE)},
                attuale: {{"colonna": 3, "lettera": "C", "intestazione": "ORDINE"}},
                scelta: "{scelta}",
              }};
            """,
        )

    def test_il_pulsante_compare_dove_c_e_una_colonna_da_spostare(self) -> None:
        html = self.esegui(
            """return renderColonnaDellOrdine(
                 {supplierId: "betulla"},
                 {orderColumn: {lettera: "C", intestazione: "ORDINE"}},
               );"""
        )

        self.assertIn("colonna <strong>C</strong>", html)
        self.assertIn('data-action="apri-colonna-ordine"', html)
        self.assertIn('data-supplier-id="betulla"', html)

    def test_e_non_compare_dove_non_c_e_niente_da_spostare(self) -> None:
        """Without a declared column there's nothing to move: a write
        configuration needs to be created first, via the guided mapping,
        along with everything else."""

        html = self.esegui(
            'return renderColonnaDellOrdine({supplierId: "quercia"}, {orderColumn: null});'
        )

        self.assertEqual(html, "")

    def test_ogni_colonna_dice_che_cosa_contiene(self) -> None:
        """The program only rejects what it can prove; the rest is left for
        whoever has the price list in front of them to judge."""

        html = self.finestra(10)

        # "6380" not "6.380": see the note in test_le_righe_scartate_si_contano_sempre.
        self.assertIn("la leggo come Codice a barre", html)
        self.assertIn("6380 formule", html)
        self.assertIn("vuota", html)
        self.assertIn("6377 valori", html)

    def test_una_colonna_gia_occupata_non_si_puo_scegliere(self) -> None:
        html = self.finestra(10)

        prima = html.split('value="1"')[1].split(">")[0]
        self.assertIn("disabled", prima)

    def test_la_colonna_gia_in_uso_non_si_riconferma(self) -> None:
        """The dialog opens on the current selection: a primary button
        enabled on a choice that changes nothing invites an accidental click."""

        html = self.finestra(3)

        pulsante = html.split('data-action="salva-colonna-ordine"')[1].split(">")[0]
        self.assertIn("disabled", pulsante)

    def test_una_colonna_non_vuota_avvisa_prima_di_scriverci(self) -> None:
        html = self.finestra(7)

        self.assertIn("non è vuota", html)
        self.assertIn("6377 valori", html)
        pulsante = html.split('data-action="salva-colonna-ordine"')[1].split(">")[0]
        self.assertNotIn("disabled", pulsante)

    def test_una_colonna_vuota_non_avvisa_di_niente(self) -> None:
        html = self.finestra(10)

        self.assertNotIn("non è vuota", html)

    def test_il_no_del_servizio_si_legge_nella_finestra(self) -> None:
        """The sentence is the real validation error — the same one that
        would show on the next startup — and must reach the user in full."""

        html = self.finestra(10, errore="BETULLA non attivato: la cella J dell'intestazione non contiene ORDINE")

        # The apostrophe goes through `escapeHtml`, so the sentence is split
        # into pieces here; what matters is that it's all there, cell name included.
        self.assertIn("BETULLA non attivato: la cella J dell", html)
        self.assertIn("intestazione non contiene ORDINE", html)
        self.assertIn("La colonna non è stata cambiata", html)


class IPulsantiSiPremonoDavvero(BancoDiProva):
    """Pressing a button does the right thing — not just "the button exists".

    The page has two click handlers, `#stepper`'s and `#app`'s (with its
    ~52 `if (action === ...)` branches). A test that only checks the markup
    would still pass if a handler were dead: `premi()` here dispatches a
    real click event and lets the real handler run, so a gutted handler
    fails these tests even though the button is still in the DOM.

    The tests below build on one invariant: during compilation, the
    supplier under a document that's being written can't be changed, and
    that guard lives in a single line inside the handler.
    """

    def test_fuori_dalla_compilazione_premere_avanti_cambia_pagina(self) -> None:
        """The counterpart of the next test: without it, a dead handler would pass."""

        passo = self.esegui(
            self.con_confronto(passo=1) + """
              premi("next");
              return state.currentStep;
            """,
            risposte={"/api/history/pending": {"pending": []}},
        )

        self.assertEqual(passo, 2)

    def test_durante_la_compilazione_premere_avanti_non_cambia_pagina(self) -> None:
        """The guard line lives in the handler; a markup-only test wouldn't run it."""

        passo = self.esegui(
            self.con_confronto(passo=1) + """
              state.compiling = true;
              premi("next");
              return state.currentStep;
            """,
        )

        self.assertEqual(passo, 1)

    def test_durante_la_compilazione_l_annulla_dello_spostamento_non_tocca_i_fornitori(self) -> None:
        fornitore = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              prodotto.selectedSupplierId = "betulla";
              state.supplierMoveUndo = { fromName: "CIPRESSO", movedCount: 1, notes: [],
                confirmationCount: 0, previous: [{ id: prodotto.id, selectedSupplierId: "cipresso", confirmed: true }] };
              state.compiling = true;
              premi("undo-supplier-move");
              return prodotto.selectedSupplierId;
            """,
        )

        self.assertEqual(fornitore, "betulla")

    def test_l_annulla_dello_spostamento_rimette_il_fornitore_e_la_conferma_di_prima(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              prodotto.selectedSupplierId = "betulla";
              prodotto.confirmed = false;
              state.supplierMoveUndo = { fromName: "CIPRESSO", movedCount: 1, notes: [],
                confirmationCount: 0, previous: [{ id: prodotto.id, selectedSupplierId: "cipresso", confirmed: true }] };
              premi("undo-supplier-move");
              return {
                fornitore: prodotto.selectedSupplierId,
                confermato: prodotto.confirmed,
                annullaRimasto: state.supplierMoveUndo,
              };
            """,
        )

        self.assertEqual(esito["fornitore"], "cipresso")
        self.assertTrue(esito["confermato"])
        # The undo is consumed: pressing it twice must not redo anything.
        self.assertIsNone(esito["annullaRimasto"])

    def test_la_conferma_di_eliminazione_chiama_la_rotta_col_nome_del_listino(self) -> None:
        """The name travels in the request body: getting it wrong deletes the wrong price list."""

        chiamate = self.esegui(
            self.con_confronto(passo=1) + """
              state.uploadDeletion.confirming = "LISTINO CIPRESSO.xlsx";
              premi("confirm-delete-upload", { uploadName: "LISTINO CIPRESSO.xlsx" });
              await attendi();
              return chiamate.map((voce) => ({
                indirizzo: voce.indirizzo,
                metodo: voce.opzioni.method || "GET",
                corpo: voce.opzioni.body || null,
              }));
            """,
            risposte={
                "/api/uploads/elimina": {"ok": True, "message": "Listino eliminato."},
                "/api/review": REVISIONE,
            },
        )

        elimina = [voce for voce in chiamate if "/api/uploads/elimina" in voce["indirizzo"]]
        self.assertEqual(len(elimina), 1, chiamate)
        self.assertEqual(elimina[0]["metodo"], "POST")
        self.assertEqual(json.loads(elimina[0]["corpo"]), {"name": "LISTINO CIPRESSO.xlsx"})

    def test_durante_il_ricalcolo_la_conferma_di_eliminazione_non_chiama_niente(self) -> None:
        """Removing a price list mid-run would change a result already in progress."""

        chiamate = self.esegui(
            self.con_confronto(passo=1, pipeline={"stato": "IN_CORSO"}) + """
              state.uploadDeletion.confirming = "LISTINO CIPRESSO.xlsx";
              premi("confirm-delete-upload", { uploadName: "LISTINO CIPRESSO.xlsx" });
              await attendi();
              return chiamate.map((voce) => voce.indirizzo);
            """,
        )

        self.assertEqual([voce for voce in chiamate if "elimina" in voce], [])

    def test_il_banco_si_accorge_se_i_gestori_spariscono(self) -> None:
        """Proof this test harness isn't inert: the mutation must break it.

        Adds `return;` as the first line of `#app`'s handler on a copy of
        `app.js`, and must produce a different result than the first test in
        this class. If it ever stopped differing, it would mean `premi()`
        had stopped actually pressing anything.
        """

        with tempfile.TemporaryDirectory() as cartella:
            mutato = Path(cartella) / "app.js"
            sorgente = APP_JS.read_text(encoding="utf-8")
            marcatore = 'appElement.addEventListener("click", (event) => {'
            self.assertIn(marcatore, sorgente)
            mutato.write_text(
                sorgente.replace(marcatore, marcatore + "\n  return;", 1), encoding="utf-8",
            )

            passo = self.esegui(
                self.con_confronto(passo=1) + """
                  premi("next");
                  return state.currentStep;
                """,
                risposte={"/api/history/pending": {"pending": []}},
                app_js=mutato,
            )

        self.assertEqual(passo, 1, "il gestore svuotato non è stato notato: premi() non preme")


class LoSpostamentoDelFornitoreSiVerificaSuiValori(BancoDiProva):
    """`tests/test_web_app.py:2094` (`SupplierMoveInterfaceTests`) checks the
    SOURCE TEXT of `applySupplierMove()`, which a `return;` at the top of the
    function would leave green: a present line isn't an executed one.

    These tests complement that check by pressing the real handler and
    asserting on the resulting VALUES instead: a `state.supplierMove` with a
    move preview and a chosen option (`choiceId`) is built, `apply-supplier-
    move` is pressed, and a gutted function fails these on the values, not
    the source.
    """

    def preventivo(self, **sovrascrizioni) -> dict:
        """A preview moving product p1 from CIPRESSO to BETULLA with the
        option already chosen (`choiceId`) — the shape `normalizeMovePreview`
        produces from the local service's response."""

        preventivo = {
            "from": "cipresso", "fromName": "CIPRESSO", "fromTotal": 30,
            "loading": False, "error": "", "choiceId": "betulla",
            "preview": {
                "from": "cipresso", "fromName": "CIPRESSO",
                "movableCount": 1, "currentNetTotal": 30,
                "options": [{
                    "id": "betulla", "kind": "supplier", "label": "BETULLA",
                    "movedCount": 1, "movableCount": 1,
                    "deltaNet": 0, "deliveredPiecesBefore": 18, "deliveredPiecesAfter": 18, "deltaPieces": 0,
                    "costPerPieceBefore": 1.67, "costPerPieceAfter": 1.67, "deltaCostPerPiece": 0,
                    "giftsBefore": 0, "giftsAfter": 0, "giftsLost": 0, "giftsGained": 0,
                    "assignments": [{
                        "productId": "p1", "productName": "Prodotto uno", "toSupplierId": "betulla",
                        "previousFactor": 6, "newFactor": 6, "factorChanged": False,
                        "previousPieces": 18, "newPieces": 18, "needsConfirmation": False,
                        "previousLineNet": 30, "newLineNet": 30,
                    }],
                    "leftBehind": [], "supplierTotalsAfter": [],
                }],
            },
        }
        preventivo.update(sovrascrizioni)
        return preventivo

    def test_lo_spostamento_cambia_davvero_il_fornitore_scelto(self) -> None:
        fornitore = self.esegui(
            self.con_confronto(passo=3) + f"""
              state.supplierMove = {json.dumps(self.preventivo())};
              premi("apply-supplier-move");
              return state.review.products[0].selectedSupplierId;
            """,
        )
        self.assertEqual(fornitore, "betulla")

    def test_lo_spostamento_azzera_la_conferma_gia_data(self) -> None:
        """Invariant: a confirmation given for one offer doesn't carry over
        to another. The product arrives confirmed for CIPRESSO."""

        confermato = self.esegui(
            self.con_confronto(passo=3) + f"""
              state.review.products[0].confirmed = true;
              state.supplierMove = {json.dumps(self.preventivo())};
              premi("apply-supplier-move");
              return state.review.products[0].confirmed;
            """,
        )
        self.assertFalse(confermato)

    def test_l_annulla_porta_le_assegnazioni_di_prima_dello_spostamento(self) -> None:
        annulla = self.esegui(
            self.con_confronto(passo=3) + f"""
              state.review.products[0].confirmed = true;
              state.supplierMove = {json.dumps(self.preventivo())};
              premi("apply-supplier-move");
              return state.supplierMoveUndo;
            """,
        )
        self.assertIsNotNone(annulla)
        self.assertEqual(
            annulla["previous"],
            [{"id": "p1", "selectedSupplierId": "cipresso", "confirmed": True}],
        )

    def test_una_funzione_svuotata_fa_cadere_queste_prove(self) -> None:
        """`return;` as the first line of `applySupplierMove()`, on a copy of
        `app.js`, must break what the tests above measure on VALUES — not
        only the source-text assertions in `test_web_app.py`."""

        with tempfile.TemporaryDirectory() as cartella:
            mutato = Path(cartella) / "app.js"
            sorgente = APP_JS.read_text(encoding="utf-8")
            marcatore = "function applySupplierMove() {"
            self.assertIn(marcatore, sorgente)
            mutato.write_text(
                sorgente.replace(marcatore, marcatore + "\n  return;", 1), encoding="utf-8",
            )

            fornitore = self.esegui(
                self.con_confronto(passo=3) + f"""
                  state.supplierMove = {json.dumps(self.preventivo())};
                  premi("apply-supplier-move");
                  return state.review.products[0].selectedSupplierId;
                """,
                app_js=mutato,
            )

        self.assertEqual(fornitore, "cipresso", "la funzione svuotata non è stata notata")


def _scappata_come_in_app_js(valore: str) -> str:
    """Python port of `escapeHtml()` in `app.js:1184`: confirms a poisoned
    string arrived HTML-escaped, not just that it went missing (which would
    pass for the wrong reason)."""

    return (
        valore.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


class LEscapeSiVerificaEseguendoLePagine(BancoDiProva):
    """`test_consegna_interfaccia.py:312`
    (`test_ogni_testo_del_servizio_passa_da_escapeHtml`) statically checks
    3 of 85 rendering functions by grepping the source around interpolations
    for `escapeHtml(...)`; a helper that escapes internally (`rigaDelFornitore`,
    `cella`, `promotionText`, `renderBadge`, `renderAlert`) would confuse
    that check and give false positives if it were widened to more functions.

    This class instead builds a `review` where every text field is a
    distinct poisoned string (so a failure names which field lost its
    escaping), renders the page's three roots (`renderUploadStep`,
    `renderQuantityStep`, `renderCompileStep`), and asserts none of the
    poisoned strings appears raw in the resulting HTML. It doesn't depend on
    which function renders what, so a new function that forgets `escapeHtml`
    is caught the same way.
    """

    CAMPI = (
        "nome del prodotto", "messaggio di conferma del prodotto", "avviso del prodotto",
        "nome del fornitore cipresso", "nome del fornitore betulla",
        "nome del documento gestionale", "nome del documento listino",
        "fornitore dichiarato dal documento", "messaggio del documento",
        "nome dell'offerta di cipresso", "descrizione dell'offerta di cipresso",
        "avviso dell'offerta di cipresso", "condizione della promozione",
        "nome dell'offerta di betulla", "descrizione dell'offerta di betulla",
    )

    def veleni(self) -> dict:
        return {campo: f'<script>alert("{campo}")</script>' for campo in self.CAMPI}

    def review_velenoso(self, v: dict) -> dict:
        return {
            "run": {"id": "R1", "status": "ready", "createdAt": "2026-08-10T09:00:00+02:00", "label": "Confronto"},
            "files": [
                {
                    "name": v["nome del documento gestionale"], "kind": "Gestionale",
                    "supplier": "Gestionale", "status": "ready", "deletable": True,
                },
                {
                    "name": v["nome del documento listino"], "kind": "Listino",
                    "supplier": v["fornitore dichiarato dal documento"], "status": "error",
                    "message": v["messaggio del documento"], "deletable": True,
                },
            ],
            "suppliers": [
                {"id": "cipresso", "name": v["nome del fornitore cipresso"], "minimumOrder": 500},
                {"id": "betulla", "name": v["nome del fornitore betulla"], "minimumOrder": 0},
            ],
            "products": [{
                "id": "p1",
                "name": v["nome del prodotto"],
                "quantity": 3,
                "selectedSupplierId": "cipresso",
                "requiresConfirmation": True,
                "confirmationMessage": v["messaggio di conferma del prodotto"],
                "warnings": [v["avviso del prodotto"]],
                "offers": [
                    {
                        "supplierId": "cipresso", "supplierName": v["nome dell'offerta di cipresso"],
                        "description": v["descrizione dell'offerta di cipresso"], "price": 10,
                        "unitsPerOrderUnit": 6, "available": True,
                        "warning": v["avviso dell'offerta di cipresso"],
                        "promotions": [{
                            "kind": "offerta_ambigua",
                            "state": {"status": "da_verificare"},
                            "source_text": v["condizione della promozione"],
                        }],
                    },
                    {
                        "supplierId": "betulla", "supplierName": v["nome dell'offerta di betulla"],
                        "description": v["descrizione dell'offerta di betulla"], "price": 12,
                        "unitsPerOrderUnit": 6, "available": True,
                    },
                ],
            }],
        }

    def disegna_le_tre_radici(self, v: dict, *, app_js: Path | None = None) -> dict:
        preparazione = f"""
          state.review = normalizeReview({json.dumps(self.review_velenoso(v))});
          state.loading = false;
          state.currentStep = 1;
          state.pipeline.stato = null;
          state.pipeline.chiesto = false;
        """
        return self.esegui(
            """
              return {
                pagina1: renderUploadStep(),
                pagina2: renderQuantityStep(),
                pagina3: renderCompileStep(),
              };
            """,
            preparazione=preparazione,
            app_js=app_js,
        )

    def test_nessuna_stringa_velenosa_esce_grezza_dalle_tre_radici_della_pagina(self) -> None:
        v = self.veleni()
        html = self.disegna_le_tre_radici(v)
        tutto = html["pagina1"] + html["pagina2"] + html["pagina3"]

        for campo, marcatore in v.items():
            with self.subTest(campo=campo):
                self.assertNotIn(marcatore, tutto, f"«{campo}» esce grezzo dalla pagina: {marcatore}")
                # Zero raw occurrences is also true of a field that got lost
                # entirely: confirm the escaped version is actually present,
                # not just that the raw one is absent.
                self.assertIn(
                    _scappata_come_in_app_js(marcatore), tutto,
                    f"«{campo}» non compare nemmeno scappato: dov'è finito?",
                )

    def test_un_escapeHtml_tolto_da_una_funzione_di_disegno_fa_cadere_la_prova(self) -> None:
        """A single `escapeHtml(...)` call is removed from `rigaDelFornitore`
        — one of the helpers the static check doesn't cover — on a copy of
        `app.js`, and the test above must notice."""

        with tempfile.TemporaryDirectory() as cartella:
            mutato = Path(cartella) / "app.js"
            sorgente = APP_JS.read_text(encoding="utf-8")
            marcatore = 'if (offer.description) pezzi.push(escapeHtml(offer.description));'
            sostituto = 'if (offer.description) pezzi.push(offer.description);'
            self.assertIn(marcatore, sorgente)
            mutato.write_text(sorgente.replace(marcatore, sostituto, 1), encoding="utf-8")

            v = self.veleni()
            html = self.disegna_le_tre_radici(v, app_js=mutato)
            tutto = html["pagina1"] + html["pagina2"] + html["pagina3"]

        self.assertIn(
            v["descrizione dell'offerta di cipresso"], tutto,
            "l'escapeHtml tolto non è stato notato: la descrizione dell'offerta esce ancora scappata",
        )


class IlXDelRiepilogoSiPuoAnnullare(BancoDiProva):
    """The summary's "×" zeroes the quantity and clears `confirmed`; since
    `orderedProducts()` only keeps products with a positive quantity, the
    product disappears from page 3 immediately — with no toast, no
    confirmation prompt, and (before this) no undo, so an accidental click
    on it went straight to "not ordered" unless someone noticed and
    re-typed the quantity from memory.
    """

    def test_premere_il_x_azzera_la_quantita_e_toglie_la_conferma(self) -> None:
        """Counterpart: without it, an undo that doesn't undo would pass."""

        esito = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              prodotto.confirmed = true;
              premi("summary-remove-product", { productId: prodotto.id });
              return { quantita: prodotto.quantity, confermato: prodotto.confirmed };
            """,
        )

        self.assertEqual(esito["quantita"], 0)
        self.assertFalse(esito["confermato"])

    def test_l_annulla_rimette_la_quantita_e_anche_la_conferma(self) -> None:
        """The confirmation too: restoring only the quantity would bring the
        product back with compilation blocked and no row explaining why."""

        esito = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              prodotto.confirmed = true;
              premi("summary-remove-product", { productId: prodotto.id });
              premi("undo-remove-product");
              return {
                quantita: prodotto.quantity,
                confermato: prodotto.confirmed,
                annullaRimasto: state.rimozioneUndo,
              };
            """,
        )

        self.assertEqual(esito["quantita"], 3)
        self.assertTrue(esito["confermato"])
        # The undo is consumed: pressing it twice must not redo anything.
        self.assertIsNone(esito["annullaRimasto"])

    def test_la_barra_di_annullo_compare_in_cima_alla_pagina_3(self) -> None:
        """At the top, not inside the list: the row the product just
        vanished from doesn't exist anymore, and a bar inserted there would
        shift whatever's being looked at. It also must not push the
        "Compile" button, at the bottom of the page, further down."""

        esito = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              premi("summary-remove-product", { productId: prodotto.id });
              const html = renderCompileStep();
              return {
                html,
                primaDelCompila: html.indexOf("undo-remove-product") < html.indexOf('data-action="compile"'),
              };
            """,
        )

        self.assertIn('data-action="undo-remove-product"', esito["html"])
        self.assertIn("Prodotto uno", esito["html"])
        self.assertTrue(esito["primaDelCompila"])

    def test_senza_rimozioni_la_barra_non_c_e(self) -> None:
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3),
        )

        self.assertNotIn("undo-remove-product", html)

    def test_un_ricalcolo_butta_via_l_annullo_di_prima(self) -> None:
        """The saved quantity belonged to the previous comparison; restoring
        it onto a new one, where the product can have different offers and
        prices, is the same trap already closed for exclusion."""

        rimasto = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              premi("summary-remove-product", { productId: prodotto.id });
              restoreExcludedProducts();
              return state.rimozioneUndo;
            """,
        )

        self.assertIsNone(rimasto)

    def test_l_annulla_toglie_anche_l_esclusione_arrivata_nel_frattempo(self) -> None:
        """Between the "×" and the undo, page 2's "Exclude from order" can be
        pressed on the same product. Restoring only the quantity would leave
        a product both "Excluded" and with a nonzero quantity: counted in
        page 3's subtotals and total, but zeroed by the local service, which
        sets excluded products' quantity to zero — a total no price list
        would ever produce."""

        esito = self.esegui(
            self.con_confronto(passo=3) + """
              const prodotto = state.review.products[0];
              premi("summary-remove-product", { productId: prodotto.id });
              premi("exclude-product", { productId: prodotto.id });
              premi("undo-remove-product");
              return {
                escluso: isExcluded(prodotto),
                quantita: prodotto.quantity,
                inOrdine: orderedProducts().length,
              };
            """,
        )

        self.assertFalse(esito["escluso"])
        self.assertEqual(esito["quantita"], 3)
        self.assertEqual(esito["inOrdine"], 1)

    def test_il_x_e_staccato_dai_tre_comandi_della_quantita(self) -> None:
        """The most destructive control shouldn't sit next to the one
        pressed most often: the three quantity controls stay in their own
        group in the markup, and the "×" stays outside it."""

        html = self.esegui(
            self.con_confronto(passo=3) + """
              return renderSummaryQuantityControls(state.review.products[0]);
            """,
        )

        gruppo = html.split('class="summary-quantity__gruppo"', 1)[1].split("</span>", 1)[0]
        self.assertIn('data-delta="-1"', gruppo)
        self.assertIn('data-delta="1"', gruppo)
        self.assertNotIn("summary-remove-product", gruppo)
        self.assertIn("summary-remove-product", html)


class IlFuocoSopravviveAlleTreDecisioni(BancoDiProva):
    """Choosing a supplier or giving a confirmation must not drop keyboard
    focus. The three decision controls — the supplier radio group, "confirm
    same item", "confirm below-minimum order" — call `render()`, which
    replaces the whole `#app` and would destroy the focused node, sending
    focus back to `<body>`. For keyboard users that breaks arrow-key
    navigation inside a radio group and forces re-tabbing from the top of
    the page after every confirmation.

    The fix is `rerenderPreservingFocus()`, which only needed a
    `data-focus-key` on the three controls to work.
    """

    def con_fuoco(self, chiave: str) -> str:
        """Pretends focus is on the node with that key, and counts whether
        anything restores it after the re-render."""

        return f"""
          globalThis.fuochi = [];
          document.activeElement = {{ dataset: {{ focusKey: "{chiave}" }} }};
          const rimesso = document.querySelector('[data-focus-key="{chiave}"]');
          rimesso.focus = () => globalThis.fuochi.push("{chiave}");
        """

    def test_scegliere_un_fornitore_rimette_il_fuoco_dove_stava(self) -> None:
        fuochi = self.esegui(
            self.con_confronto(passo=2)
            + self.con_fuoco("offerta-p1-cipresso")
            + """
              cambia({ offerChoice: "p1" }, { value: "betulla" });
              return globalThis.fuochi;
            """,
        )

        self.assertEqual(fuochi, ["offerta-p1-cipresso"])

    def test_la_conferma_del_prodotto_rimette_il_fuoco_dove_stava(self) -> None:
        """The confirmation is a button, not a checkbox, but the invariant
        is the same: pressing it must not drop focus. The button doesn't
        disappear — it becomes "Cambio idea" and keeps the same key — so
        focus lands back in exactly the same place."""

        fuochi = self.esegui(
            self.con_confronto(passo=2)
            + self.con_fuoco("conferma-p1")
            + """
              premi("conferma-abbinamento", { productId: "p1", confermato: "true" });
              return globalThis.fuochi;
            """,
        )

        self.assertEqual(fuochi, ["conferma-p1"])

    def test_la_conferma_sotto_il_minimo_rimette_il_fuoco_dove_stava(self) -> None:
        fuochi = self.esegui(
            self.con_confronto(passo=3)
            + self.con_fuoco("sotto-minimo")
            + """
              cambia({ belowThresholdConfirm: "" }, { checked: true });
              return globalThis.fuochi;
            """,
        )

        self.assertEqual(fuochi, ["sotto-minimo"])

    def test_le_tre_chiavi_stanno_davvero_nel_markup(self) -> None:
        """Without the key in the markup, `rerenderPreservingFocus()` finds
        nothing and no-ops: the three tests above would pass against a node
        the browser would never render."""

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              const prodotto = state.review.products[0];
              prodotto.requiresConfirmation = true;
              state.currentStep = 3;
              const pagina3 = renderCompileStep();
              state.currentStep = 2;
              return { pagina2: renderQuantityStep(), pagina3 };
            """,
        )

        self.assertIn('data-focus-key="offerta-p1-cipresso"', esito["pagina2"])
        self.assertIn('data-focus-key="conferma-p1"', esito["pagina2"])
        self.assertIn('data-focus-key="sotto-minimo"', esito["pagina3"])

    def test_se_il_controllo_sparisce_col_ridisegno_non_si_rompe_niente(self) -> None:
        """Changing supplier can make the confirmation unnecessary, so it
        disappears: `rerenderPreservingFocus()` finds no node, no-ops, and
        focus stays lost as before — not worse."""

        esito = self.esegui(
            self.con_confronto(passo=2)
            + self.con_fuoco("conferma-p1")
            + """
              // ⚠ Il nodo NON deve esistere: senza questa riga il banco lo
              // creerebbe al volo e il ramo «non l'ho trovato» non lo
              // eseguirebbe nessuno.
              dichiaraAssente('[data-focus-key="conferma-p1"]');
              globalThis.ridisegni = 0;
              const vero = render;
              render = (...a) => { globalThis.ridisegni += 1; return vero(...a); };
              premi("conferma-abbinamento", { productId: "p1", confermato: "true" });
              return { fuochi: globalThis.fuochi, ridisegni: globalThis.ridisegni };
            """,
        )

        # Focus is not restored anywhere…
        self.assertEqual(esito["fuochi"], [])
        # …but the page still re-rendered: falling back to the old
        # behavior, not to something worse.
        self.assertEqual(esito["ridisegni"], 1)


class IlFuocoSopravviveAiPulsantiCheRidisegnano(BancoDiProva):
    """Focus must survive every re-render caused by a click, not just a
    keystroke: `data-focus-key` needs to be on `<button>` elements too, not
    just fields the user types into, or pressing "+" or "-" drops focus and
    the next Tab restarts from the top of the document. The same problem
    would hit once a second for the whole duration of a comparison run,
    because the progress indicator re-renders with `render()`.
    """

    def con_fuoco(self, chiave: str) -> str:
        return f"""
          globalThis.fuochi = [];
          document.activeElement = {{ dataset: {{ focusKey: "{chiave}" }} }};
          const rimesso = document.querySelector('[data-focus-key="{chiave}"]');
          rimesso.focus = () => globalThis.fuochi.push("{chiave}");
        """

    def test_il_piu_della_pagina_2_rimette_il_fuoco_su_se_stesso(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=2)
            + self.con_fuoco("qty-piu-p1")
            + """
              premi("change-quantity", { productId: "p1", delta: "1" });
              return { fuochi: globalThis.fuochi, quantita: state.review.products[0].quantity };
            """,
        )

        self.assertEqual(esito["quantita"], 4)
        self.assertEqual(esito["fuochi"], ["qty-piu-p1"])

    def test_il_meno_del_riepilogo_rimette_il_fuoco_su_se_stesso(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=3)
            + self.con_fuoco("riepilogo-meno-p1")
            + """
              premi("change-quantity", { productId: "p1", delta: "-1" });
              return { fuochi: globalThis.fuochi, quantita: state.review.products[0].quantity };
            """,
        )

        self.assertEqual(esito["quantita"], 2)
        self.assertEqual(esito["fuochi"], ["riepilogo-meno-p1"])

    def test_le_chiavi_dei_cinque_pulsanti_stanno_nel_markup(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=2) + """
              const pagina2 = renderQuantityStep();
              state.currentStep = 3;
              const pagina3 = renderCompileStep();
              return { pagina2, pagina3 };
            """,
        )

        for chiave in ("qty-meno-p1", "qty-piu-p1"):
            self.assertIn(f'data-focus-key="{chiave}"', esito["pagina2"])
        for chiave in ("riepilogo-meno-p1", "riepilogo-piu-p1", "riepilogo-togli-p1"):
            self.assertIn(f'data-focus-key="{chiave}"', esito["pagina3"])

    def test_il_controllo_dell_avanzamento_non_butta_via_il_fuoco_ogni_secondo(self) -> None:
        """During a comparison run the re-render happens every second: with
        `render()`, focus would have returned to `<body>` sixty times a minute."""

        fuochi = self.esegui(
            self.con_confronto(passo=1)
            + self.con_fuoco("product-search")
            + """
              await controllaPipeline();
              return globalThis.fuochi;
            """,
            risposte={"/api/pipeline/stato": {"stato": "IN_CORSO", "messaggio": "Lettura dei documenti…",
                                              "avanzamento": {"percento": 22.2}, "fasi": []}},
        )

        self.assertEqual(fuochi, ["product-search"])


class LeQuattroFinestreSiComportanoAlloStessoModo(BancoDiProva):
    """The four dialogs — order column, price-list viewer, catalog, supplier
    move — all declare `aria-modal="true"` and must all close on Esc, not
    just via the "×". The price-list viewer matters most here: it's the
    escape hatch for when automatic matching failed, so it opens exactly
    when something has already gone wrong.
    """

    def test_esc_chiude_il_catalogo(self) -> None:
        aperto = self.esegui(
            self.con_confronto(passo=2) + """
              state.catalog.open = true;
              tasto("Escape");
              return state.catalog.open;
            """,
        )
        self.assertFalse(aperto)

    def test_esc_chiude_lo_spostamento_fra_fornitori(self) -> None:
        da = self.esegui(
            self.con_confronto(passo=3) + """
              state.supplierMove.from = "cipresso";
              tasto("Escape");
              return state.supplierMove.from;
            """,
        )
        self.assertFalse(bool(da))

    def test_esc_chiude_il_visualizzatore_listini(self) -> None:
        """The escape hatch for a failed automatic match: closing it must not depend on the mouse."""

        aperto = self.esegui(
            self.con_confronto(passo=2) + """
              state.listino.aperto = true;
              tasto("Escape");
              return state.listino.aperto;
            """,
        )
        self.assertFalse(aperto)

    def test_esc_chiude_la_finestra_della_colonna_d_ordine(self) -> None:
        """The most consequential of the four: it decides which column of
        the supplier's price list the order quantities are written into,
        so closing it must not depend on the mouse."""

        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.colonnaOrdine.aperta = true;
              tasto("Escape");
              return state.colonnaOrdine.aperta;
            """,
        )
        self.assertFalse(aperta)

    def test_esc_chiude_quella_in_cima_e_lascia_stare_le_altre(self) -> None:
        """Order follows the markup: `render()` appends the order-column
        dialog after any page's content, so it's always rendered last and
        closes first."""

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              state.catalog.open = true;
              state.colonnaOrdine.aperta = true;
              tasto("Escape");
              return { colonna: state.colonnaOrdine.aperta, catalogo: state.catalog.open };
            """,
        )
        self.assertFalse(esito["colonna"])
        self.assertTrue(esito["catalogo"])

    def test_esc_senza_finestre_aperte_non_fa_niente(self) -> None:
        passo = self.esegui(
            self.con_confronto(passo=2) + """
              tasto("Escape");
              return state.currentStep;
            """,
        )
        self.assertEqual(passo, 2)

    def test_all_apertura_il_fuoco_entra_nella_finestra(self) -> None:
        """All four dialogs must do this, as the catalog already did.
        `autofocus` alone isn't enough: it isn't honored on nodes inserted
        via `innerHTML`, which is why the catalog defers it with a `setTimeout`."""

        fuochi = self.esegui(
            self.con_confronto(passo=2) + """
              globalThis.fuochi = [];
              for (const selettore of ["#catalog-search", "#listino-cerca"]) {
                nodoDi(selettore).focus = () => globalThis.fuochi.push(selettore);
              }
              openCatalog();
              apriIlListino("p1", "cipresso");
              return globalThis.fuochi;
            """,
            risposte={"/api/listino": {"righe": [], "fornitori": [], "totale": 0, "trovate": 0, "scartate": 0}},
        )

        self.assertEqual(fuochi, ["#catalog-search", "#listino-cerca"])

    def test_alla_chiusura_il_fuoco_torna_sul_comando_che_l_aveva_aperta(self) -> None:
        """All four dialogs must restore focus on close, or it stays stuck
        on a node the closed dialog already removed from the DOM."""

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              globalThis.fuochi = [];
              document.activeElement = { dataset: { focusKey: "apri-catalogo" } };
              nodoDi('[data-focus-key="apri-catalogo"]').focus = () => globalThis.fuochi.push("comando");
              openCatalog();
              premi("close-catalog");
              return { fuochi: globalThis.fuochi, aperto: state.catalog.open };
            """,
        )

        self.assertFalse(esito["aperto"])
        self.assertEqual(esito["fuochi"], ["comando"])

    def test_se_il_comando_non_c_e_piu_il_fuoco_ripiega_sul_contenuto(self) -> None:
        """The "browse price list" command can disappear after a manual
        match changes the product's card: without a fallback, focus would
        be left with nowhere to go."""

        fuochi = self.esegui(
            self.con_confronto(passo=2) + """
              globalThis.fuochi = [];
              document.activeElement = { dataset: {} };
              nodoDi("#workspace").focus = () => globalThis.fuochi.push("workspace");
              openCatalog();
              premi("close-catalog");
              return globalThis.fuochi;
            """,
        )

        self.assertEqual(fuochi, ["workspace"])

    def test_i_quattro_comandi_che_aprono_hanno_una_chiave_del_fuoco(self) -> None:
        """Without the key there's nothing to remember, and focus would
        always fall back to `#workspace`."""

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              const pagina2 = renderQuantityStep();
              state.currentStep = 3;
              const pagina3 = renderCompileStep();
              return { pagina2, pagina3 };
            """,
        )

        self.assertIn('data-focus-key="apri-catalogo"', esito["pagina2"])
        self.assertIn('data-focus-key="apri-listino-p1"', esito["pagina2"])
        self.assertIn('data-focus-key="sposta-cipresso"', esito["pagina3"])
        # The fourth opens the most consequential of the four dialogs: it
        # decides which price-list column the order quantities are written into.
        colonna = self.esegui(
            self.con_confronto(passo=1) + """
              const file = { name: "listino.xlsx", supplierId: "cipresso", kind: "Listino" };
              return renderColonnaDellOrdine(file, { orderColumn: { lettera: "P", intestazione: "Ordine" } });
            """,
        )
        self.assertIn('data-focus-key="colonna-ordine-cipresso"', colonna)


class LoSfondoChiudeLaFinestra(BancoDiProva):
    """All four dialogs must close on a backdrop click, the gesture most
    people try first.

    The backdrop click does NOT go through `data-action`: `closest()` would
    walk up from the clicked point to the backdrop, so a click inside the
    dialog that lands on a margin (between two fields, next to a title)
    would close it too. In the order-column dialog that would mean losing
    the choice just made.
    """

    def test_cliccare_lo_sfondo_chiude_la_finestra_in_cima(self) -> None:
        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.colonnaOrdine.aperta = true;
              cliccaSu("dialog-backdrop");
              return state.colonnaOrdine.aperta;
            """,
        )
        self.assertFalse(aperta)

    def test_un_clic_dentro_la_finestra_non_la_chiude(self) -> None:
        """The trap: a click target that isn't the backdrop must not close
        anything, even if the backdrop is one of its ancestors."""

        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.colonnaOrdine.aperta = true;
              cliccaSu("colonna-dialog");
              return state.colonnaOrdine.aperta;
            """,
        )
        self.assertTrue(aperta)

    def test_tutte_e_quattro_le_finestre_portano_lo_sfondo_che_il_gestore_cerca(self) -> None:
        """Without this, the handler and the markup could silently drift
        apart: the other tests in this class fabricate the backdrop class
        themselves. Renaming it in the markup alone would keep the suite
        green while the backdrop stopped closing dialogs in the browser —
        and lose the dimming overlay too, which shares the same class."""

        quante = self.esegui(
            self.con_confronto(passo=2) + """
              state.catalog.open = true;
              state.listino.aperto = true;
              state.colonnaOrdine.aperta = true;
              state.supplierMove.from = "cipresso";
              state.supplierMove.fromName = "CIPRESSO";
              const tutte = renderCatalogDialog() + renderListinoDialog()
                + renderColonnaOrdineDialog() + renderSupplierMoveDialog();
              return (tutte.match(/class="dialog-backdrop"/g) || []).length;
            """,
        )

        self.assertEqual(quante, 4)

    def test_cliccare_lo_sfondo_senza_finestre_non_fa_niente(self) -> None:
        passo = self.esegui(
            self.con_confronto(passo=2) + """
              cliccaSu("dialog-backdrop");
              return state.currentStep;
            """,
        )
        self.assertEqual(passo, 2)

    def test_lo_sfondo_restituisce_il_fuoco_come_fanno_esc_e_la_crocetta(self) -> None:
        fuochi = self.esegui(
            self.con_confronto(passo=2) + """
              globalThis.fuochi = [];
              document.activeElement = { dataset: { focusKey: "apri-catalogo" } };
              nodoDi('[data-focus-key="apri-catalogo"]').focus = () => globalThis.fuochi.push("comando");
              openCatalog();
              cliccaSu("dialog-backdrop");
              return globalThis.fuochi;
            """,
        )
        self.assertEqual(fuochi, ["comando"])


class BloccanteEAvvisoSiDistinguonoSenzaGuardareIlColore(BancoDiProva):
    """A warning and a blocker must be distinguishable without relying on
    the color of the 4px side stripe alone — amber vs. red, 1.33:1 contrast
    between them. For someone with red-green color deficiency, or on a
    store's monitor, they read as the same stripe, giving no way to spot
    the one card that blocks compilation among twenty. The metadata row
    already counts the issues, but "3 issues" reads the same whether one of
    the three blocks compilation or not.
    """

    def scheda(self, *, bloccante: bool) -> str:
        # The real blocker: a required confirmation that hasn't been given
        # blocks compilation. A product with no offers does NOT block it —
        # that's a deliberate choice, encoded in collectIssues.
        con_bloccante = """
              const prodotto = state.review.products[0];
              prodotto.requiresConfirmation = true;
              prodotto.confirmed = false;
        """
        con_avviso = """
              const prodotto = state.review.products[0];
              prodotto.warnings = [{ id: "w1", severity: "warning", blocking: false,
                productId: prodotto.id, title: "Da guardare", message: "Un controllo." }];
        """
        return self.esegui(
            self.con_confronto(passo=2)
            + (con_bloccante if bloccante else con_avviso)
            + """
              invalidateIssues();
              return renderCompactProduct(state.review.products[0]);
            """,
        )

    def test_un_bloccante_lo_dice_a_parole(self) -> None:
        html = self.scheda(bloccante=True)

        self.assertIn("has-blocker", html)
        self.assertIn("Da sistemare", html)
        self.assertNotIn("Da controllare", html)

    def test_un_avviso_lo_dice_a_parole_e_non_dice_da_sistemare(self) -> None:
        html = self.scheda(bloccante=False)

        self.assertIn("has-warning", html)
        self.assertIn("Da controllare", html)
        self.assertNotIn("Da sistemare", html)

    def test_una_scheda_senza_controlli_non_porta_nessuna_delle_due(self) -> None:
        html = self.esegui(
            self.con_confronto(passo=2) + """
              invalidateIssues();
              return renderCompactProduct(state.review.products[0]);
            """,
        )

        self.assertNotIn("Da sistemare", html)
        self.assertNotIn("Da controllare", html)
        self.assertNotIn("has-blocker", html)
        self.assertNotIn("has-warning", html)

    def test_il_colore_non_e_piu_il_solo_portatore_della_distinzione(self) -> None:
        """CSS rule: no side stripe as the sole signal, on the card or on
        the changed-documents strip."""

        import re

        css = re.sub(r"\s+", " ", STYLES.read_text(encoding="utf-8"))

        # No colored side stripe, however it's written.
        self.assertNotRegex(css, r"border-left: *[2-9]px +solid")
        # And the replacement reinforcement is actually there: full border
        # and tinted background on both states.
        for regola in (".product-card.has-warning {", ".product-card.has-blocker {"):
            corpo = css.split(regola, 1)[1].split("}", 1)[0]
            self.assertIn("border-color:", corpo, regola)
            self.assertIn("background:", corpo, regola)


class IlConfrontoDiceDiQuantoUnFornitoreCostaDiPiu(BancoDiProva):
    """The cheapest-per-piece row is already preselected, sorted first, and
    green-bordered, but a column stating BY HOW MUCH the others cost more
    is what turns two-number mental subtraction into a glance. The delta
    must also survive a manual choice of a pricier supplier, when the green
    border moves off the cheapest row.
    """

    DUE_OFFERTE = {
        "run": {"id": "R1", "status": "ready", "createdAt": "2026-08-10T09:00:00+02:00", "label": "Confronto"},
        "files": [],
        "suppliers": [
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 0},
            {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
        ],
        "products": [{
            "id": "p1",
            "name": "Prodotto uno",
            "quantity": 1,
            "selectedSupplierId": "cipresso",
            "offers": [
                {"supplierId": "cipresso", "price": 24, "unitsPerOrderUnit": 24, "available": True},
                {"supplierId": "betulla", "price": 26.4, "unitsPerOrderUnit": 24, "available": True},
            ],
        }],
    }

    def griglia(self, revisione=None) -> str:
        return self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2, revisione=revisione or self.DUE_OFFERTE),
        )

    def test_il_fornitore_piu_caro_dice_di_quanto_costa_di_piu(self) -> None:
        html = self.griglia()

        # 1.10 per piece vs. 1.00: ten cents.
        self.assertIn("+0,10\xa0€/pz", html)

    def test_il_piu_conveniente_non_porta_nessuna_differenza(self) -> None:
        """Adding a "cheapest" delta would duplicate a signal already given
        three times — it's first, preselected, and green-bordered — and on
        a manual choice would put two markers on different rows."""

        html = self.griglia()
        prima_riga = html.split('<label class="offer-card', 2)[1]
        # Only the table rows: the summary panel's own closing line says
        # "you have the cheapest per piece", which is a separate statement
        # about the choice, not a per-row marker.
        griglia = html.split('<div class="offer-grid">', 1)[1]

        self.assertIn("CIPRESSO", prima_riga)
        self.assertNotIn("offer-card__delta", prima_riga)
        self.assertNotIn("conveniente", griglia)

    def test_la_differenza_resta_anche_se_si_sceglie_a_mano_il_piu_caro(self) -> None:
        """The green border moves to the manual choice, leaving the
        cheapest option unmarked unless the delta stays too."""

        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["selectedSupplierId"] = "betulla"
        html = self.griglia(revisione)

        self.assertIn("+0,10\xa0€/pz", html)

    def test_il_minimo_si_prende_fra_le_sole_offerte_disponibili(self) -> None:
        """Otherwise, for a product whose cheapest offer isn't usable, the
        page would compute deltas against a price nobody can order."""

        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["offers"].append(
            {"supplierId": "larice", "price": 12, "unitsPerOrderUnit": 24,
             "available": False, "warning": "Fuori assortimento."},
        )
        revisione["suppliers"].append({"id": "larice", "name": "LARICE", "minimumOrder": 0})
        html = self.griglia(revisione)

        # The reference stays 1.00 (CIPRESSO), not 0.50 (LARICE, unavailable).
        self.assertIn("+0,10\xa0€/pz", html)
        self.assertNotIn("+0,60\xa0€/pz", html)

    def test_due_offerte_allo_stesso_prezzo_non_scrivono_zero(self) -> None:
        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["offers"][1]["price"] = 24
        html = self.griglia(revisione)

        self.assertNotIn("+0,00", html)
        self.assertNotIn("offer-card__delta", html)

    def test_la_pagina_dice_una_volta_sola_com_e_ordinato_l_elenco(self) -> None:
        """Once per page, not under every grid: repeating the "Confronto
        dei fornitori" heading on every card would mean twenty copies of
        the same sentence per page."""

        # TWO products: with only one, "once per page" and "once per card"
        # would give the same count and the test wouldn't distinguish the
        # regression it's meant to catch.
        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        secondo = json.loads(json.dumps(revisione["products"][0]))
        secondo["id"] = "p2"
        secondo["name"] = "Prodotto due"
        revisione["products"].append(secondo)
        html = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(passo=2, revisione=revisione),
        )

        self.assertIn("product-card", html)
        self.assertEqual(html.count('data-product-id="p2"') > 0, True)
        self.assertEqual(html.count("dal più conveniente al pezzo"), 1)


class IDocumentiScartatiRestanoScritti(BancoDiProva):
    """A file rejected at upload (.xlsm, .ods, .pdf, ...) must stay named
    and visible under the upload box, not just flash by in a transient
    toast: `state.pendingFiles` holds only accepted files, so the rejected
    ones need their own list to show up at all. Dropping five price lists
    and having one silently rejected could mean running the comparison
    without that supplier — an order sent to whoever happens to cost more.
    """

    def con_file(self, nomi: list, ruolo: str = "suppliers") -> str:
        elenco = ", ".join(
            f'{{ name: "{nome}", size: 10, lastModified: 1 }}' for nome in nomi
        )
        return f"""
          addPendingFiles([{elenco}], "{ruolo}");
        """

    def test_uno_scartato_resta_nell_elenco_col_nome_e_col_motivo(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["LISTINO BETULLA.xlsx", "CATALOGO.pdf"])
            + """
              return { scartati: state.fileScartati, html: renderUploadStep() };
            """,
        )

        self.assertEqual(len(esito["scartati"]), 1)
        self.assertEqual(esito["scartati"][0]["nome"], "CATALOGO.pdf")
        self.assertIn("CATALOGO.pdf", esito["html"])
        self.assertIn(".pdf non è un formato che so leggere", esito["html"])

    def test_un_file_senza_estensione_non_dice_una_cosa_falsa(self) -> None:
        motivo = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["listino"])
            + """
              return state.fileScartati[0].motivo;
            """,
        )

        self.assertIn("senza estensione", motivo)

    def test_gli_scartati_non_entrano_fra_quelli_da_spedire(self) -> None:
        """The test that matters: `pendingFiles` is the array the request
        to the local service is built from."""

        esito = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["LISTINO BETULLA.xlsx", "CATALOGO.pdf", "vecchio.ods"])
            + """
              return {
                daSpedire: state.pendingFiles.map((voce) => voce.file.name),
                scartati: state.fileScartati.map((voce) => voce.nome),
              };
            """,
        )

        self.assertEqual(esito["daSpedire"], ["LISTINO BETULLA.xlsx"])
        self.assertEqual(sorted(esito["scartati"]), ["CATALOGO.pdf", "vecchio.ods"])

    def test_lo_stesso_documento_trascinato_due_volte_compare_una_volta(self) -> None:
        quanti = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["CATALOGO.pdf"])
            + self.con_file(["CATALOGO.pdf"])
            + """
              return state.fileScartati.length;
            """,
        )

        self.assertEqual(quanti, 1)

    def test_si_tolgono_con_un_comando_loro_e_non_con_quello_degli_accettati(self) -> None:
        """`remove-file` passes the index into `state.pendingFiles`: reusing it
        here would remove a good document instead of a rejected one."""

        esito = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["LISTINO BETULLA.xlsx", "CATALOGO.pdf"])
            + """
              premi("togli-scartato", { scartatoIndice: "0" });
              return {
                scartati: state.fileScartati.length,
                daSpedire: state.pendingFiles.length,
              };
            """,
        )

        self.assertEqual(esito["scartati"], 0)
        self.assertEqual(esito["daSpedire"], 1)

    def test_uno_scartato_si_vede_anche_se_nessun_documento_buono_e_in_attesa(self) -> None:
        """Worst case: only one file is dropped, and it's the wrong one.
        `renderPendingFiles` must not bail out just because there are no
        accepted documents to show."""

        html = self.esegui(
            self.con_confronto(passo=1)
            + self.con_file(["CATALOGO.pdf"])
            + """
              return renderUploadStep();
            """,
        )

        self.assertIn("CATALOGO.pdf", html)
        self.assertIn("file-scartati", html)


class LaBarraDelRicalcoloDiceDaQuantoGira(BancoDiProva):
    """Progress is "phases completed / nine", all weighted equally.
    `VALUTAZIONE_AI` is the sixth phase and, by the code's own accounting,
    the slowest by far: the bar climbs in steps to 55.6% and then sits still
    for most of the run. To a non-technical user, "stuck at 56% for two
    minutes" reads as "it crashed", and the natural reaction is to close the
    program while the pipeline is still working — so the elapsed time is
    shown alongside the percentage.

    Elapsed time is measured from the service's own timestamp, not a
    `Date.now()` fixed in the page: a browser reload mid-run must not reset
    the counter to zero and understate how long it's been running.
    """

    def in_corso(self, minuti_fa: float) -> dict:
        return {
            "stato": "IN_CORSO",
            "messaggio": "Valutazione dei candidati…",
            "iniziatoIl": f"__INIZIO_{minuti_fa}__",
            "avanzamento": {"fatte": 5, "totali": 9, "percento": 55.6},
            "fasi": [],
        }

    def disegna(self, minuti_fa: float) -> str:
        pipeline = self.in_corso(minuti_fa)
        preparazione = self.con_confronto(pipeline=pipeline, passo=1).replace(
            f'"__INIZIO_{minuti_fa}__"',
            f"new Date(Date.now() - {minuti_fa} * 60000).toISOString()",
        )
        return self.esegui("return renderAvanzamentoPipeline();", preparazione=preparazione)

    def test_dopo_due_minuti_lo_dice(self) -> None:
        html = self.disegna(2.4)

        self.assertIn("In corso da 2 minuti.", html)

    def test_dopo_un_minuto_dice_minuto_al_singolare(self) -> None:
        html = self.disegna(1.2)

        self.assertIn("In corso da 1 minuto.", html)

    def test_sotto_il_minuto_non_dice_niente(self) -> None:
        """Up to that point the bar is still moving on its own: a counter
        at zero would just be noise."""

        html = self.disegna(0.3)

        self.assertNotIn("In corso da", html)

    def test_non_c_e_nessun_tetto_perche_e_li_che_serve(self) -> None:
        """"In corso da 47 minuti" is exactly the message needed when
        something has actually gone wrong."""

        html = self.disegna(47)

        self.assertIn("In corso da 47 minuti.", html)

    def test_a_ricalcolo_finito_il_contatore_sparisce(self) -> None:
        html = self.esegui(
            "return renderAvanzamentoPipeline();",
            preparazione=self.con_confronto(
                pipeline={"stato": "COMPLETATO", "messaggio": "Confronto aggiornato.",
                          "iniziatoIl": "2026-08-10T09:00:00+02:00", "fasi": []},
                passo=1,
            ),
        )

        self.assertNotIn("In corso da", html)

    def test_senza_il_timbro_del_servizio_non_si_inventa_niente(self) -> None:
        html = self.esegui(
            "return renderAvanzamentoPipeline();",
            preparazione=self.con_confronto(
                pipeline={"stato": "IN_CORSO", "messaggio": "Lettura…", "fasi": [],
                          "avanzamento": {"percento": 11.1}},
                passo=1,
            ),
        )

        self.assertNotIn("In corso da", html)


class PrimaDiCompilareSiSaQuanteCopieVerrannoCreate(BancoDiProva):
    """"Compila i listini" (compile the price lists) creates copies of files
    on disk and logs a compilation in the history — the only action on the
    page that leaves a trace outside the program. Before pressing it, the
    card described what happens in general and showed a total, but not how
    many copies or for which suppliers — the number of documents about to
    exist. And afterward, `state.compilazioni.aperta` starts `false`, so the
    panel saying WHERE they ended up stayed closed, one click away and with
    nothing suggesting it.
    """

    def test_la_riga_dice_quante_copie_e_per_chi(self) -> None:
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3),
        )

        # The past participle also stays singular: `contati()` inflects the
        # noun, not the verb in front of it.
        self.assertIn("Sarà creata 1 copia: CIPRESSO.", html)
        self.assertNotIn("Saranno create 1", html)

    def test_con_due_fornitori_dice_due_copie_e_li_nomina_tutti(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"].append({
            "id": "p2", "name": "Prodotto due", "quantity": 2, "selectedSupplierId": "betulla",
            "offers": [{"supplierId": "betulla", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
        })
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3, revisione=revisione),
        )

        self.assertIn("Saranno create 2 copie: CIPRESSO e BETULLA.", html)

    def test_senza_niente_da_ordinare_la_riga_non_c_e(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["quantity"] = 0
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3, revisione=revisione),
        )

        self.assertNotIn("Saranno create", html)

    def test_la_riga_non_ripete_i_subtotali_che_stanno_gia_sopra(self) -> None:
        """Count and names only: subtotals and totals are already shown
        elsewhere, and repeating them here would just be duplicated noise."""

        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3),
        )
        riga = html.split('class="compile-card__copie"', 1)[1].split("</p>", 1)[0]

        self.assertNotIn("€", riga)
        self.assertNotIn("riga", riga)


class LaSpuntaSuiPassiNonMentePiu(BancoDiProva):
    """The green checkmark means "done", never just "you've been here": a
    purely positional rule like `state.currentStep > step.id` would paint
    step 1 green and checked as soon as the service responds and "2.
    Scegli" is clickable, even with zero price lists imported. Going back
    from page 3 to page 1 must also keep the checkmark on steps 2 and 3
    when quantities and suppliers are already chosen. To a non-technical
    user, the green checkmark IS the claim that a step is complete.
    """

    def passi(self, preparazione: str) -> list:
        """Reads the MARKUP, not `passoCompletato()`: a test asserting on
        the helper function alone would still pass with the old, purely
        positional rule reinstated in `renderStepper`."""

        html = self.esegui(
            preparazione + """
              renderStepper();
              return document.querySelector("#stepper").innerHTML;
            """,
        )
        voci = html.split("<li ")[1:]
        assert len(voci) >= 3, html
        return [("stepper__fatto" in voce) for voce in voci[:3]]

    def test_stare_sulla_pagina_2_non_dichiara_fatto_il_passo_1(self) -> None:
        """Counterpart of the old behavior: with the previous rule this
        would have been [true, false, false] from position alone."""

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"] = []
        fatti = self.passi(self.con_confronto(passo=2, revisione=revisione))

        self.assertEqual(fatti, [False, False, False])

    def test_il_passo_1_e_fatto_quando_un_confronto_esiste(self) -> None:
        fatti = self.passi(self.con_confronto(passo=1))

        self.assertTrue(fatti[0])

    def test_il_passo_2_e_fatto_quando_c_e_almeno_una_quantita(self) -> None:
        fatti = self.passi(self.con_confronto(passo=1))
        self.assertTrue(fatti[1])

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["quantity"] = 0
        senza = self.passi(self.con_confronto(passo=3, revisione=revisione))
        self.assertFalse(senza[1])

    def test_tornare_indietro_non_toglie_i_fatti_gia_fatti(self) -> None:
        """Going from page 3 back to page 1 must not drop the checkmark
        from steps 2 and 3 while quantities and suppliers are still chosen."""

        avanti = self.passi(self.con_confronto(passo=3))
        indietro = self.passi(self.con_confronto(passo=1))

        self.assertEqual(avanti[:2], indietro[:2])

    def test_il_terzo_passo_non_dichiara_nessun_fatto(self) -> None:
        """Step 3 is where compilation happens, and compilation has its own
        result panel: a checkmark on this step would have nothing to state."""

        fatti = self.passi(self.con_confronto(passo=3))

        self.assertFalse(fatti[2])


class UnSoloComandoPrimarioPerPagina(BancoDiProva):
    """"One primary command per page" is a project rule that wasn't
    followed in two places: Settings had two blue buttons — "Salva la
    chiave" mid-page and "Salva modello e limiti" at the bottom — that save
    different things, and the step bar, which lives outside `#app`, kept
    declaring the page navigated FROM as active — so while scrolled into
    the equality declarations list, the only fixed element on screen still
    read "Importa i dati".
    """

    def test_in_impostazioni_c_e_un_solo_pulsante_blu(self) -> None:
        html = self.esegui(
            self.con_confronto(passo=1) + """
              state.impostazioni.aperta = true;
              state.impostazioni.nuovaChiave = "sk-prova";
              return renderSettingsPage();
            """,
        )

        self.assertEqual(html.count("button--primary"), 1)
        self.assertIn("Salva modello e limiti", html.split("button--primary", 1)[1][:220])
        self.assertIn("Salva la chiave", html)

    def test_con_le_impostazioni_aperte_la_barra_dei_passi_lo_dice(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=1) + """
              state.impostazioni.aperta = true;
              renderStepper();
              const conImpostazioni = document.querySelector("#stepper").innerHTML;
              state.impostazioni.aperta = false;
              renderStepper();
              return { conImpostazioni, senza: document.querySelector("#stepper").innerHTML };
            """,
        )

        self.assertIn("Impostazioni", esito["conImpostazioni"])
        self.assertIn('aria-current="page"', esito["conImpostazioni"])
        # And none of the three steps stays marked as the current one.
        self.assertNotIn('aria-current="step"', esito["conImpostazioni"])
        # Closed, the entry disappears and the step reverts to being current.
        self.assertNotIn("Impostazioni", esito["senza"])
        self.assertIn('aria-current="step"', esito["senza"])

    def test_premere_la_voce_impostazioni_le_chiude(self) -> None:
        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.impostazioni.aperta = true;
              renderStepper();
              // ⚠ SENZA `step`: con un `data-step`, anche vuoto, scatta il
              // ramo vecchio e `goToStep()` chiude le impostazioni da solo —
              // la prova sarebbe verde anche senza la voce nuova.
              premiNellaBarra({ action: "chiudi-impostazioni" });
              return state.impostazioni.aperta;
            """,
        )

        self.assertFalse(aperta)


class L_AzzeramentoInBloccoSiPuoAnnullare(BancoDiProva):
    """"Azzera le quantità predefinite" (reset the suggested quantities) is
    a bulk command that needs confirmation, undo, and a label that says
    what it touches. The code only resets products with `quantitySource
    === "gestionale"` — the ones never touched by hand, since editing a
    quantity flips its source to "utente" — and the label states exactly
    that, matching the risk to a command this destructive: excluding a
    single product, a smaller action, already has its own "Undo" strip.
    """

    def con_due_origini(self) -> dict:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["quantitySource"] = "gestionale"
        revisione["products"].append({
            "id": "p2", "name": "Prodotto due", "quantity": 7, "quantitySource": "utente",
            "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
        })
        return revisione

    def test_azzera_solo_le_quantita_del_gestionale(self) -> None:
        quantita = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              premi("reset-suggested-quantities");
              return state.review.products.map((prodotto) => prodotto.quantity);
            """,
        )

        self.assertEqual(quantita, [0, 7])

    def test_l_annulla_rimette_le_quantita_e_la_loro_provenienza(self) -> None:
        """The source reverts to "gestionale": if it stayed "utente" the
        button wouldn't see those quantities anymore, and pressing it again
        would do nothing."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              state.review.products[0].confirmed = true;
              premi("reset-suggested-quantities");
              premi("undo-azzeramento");
              const primo = state.review.products[0];
              return {
                quantita: primo.quantity,
                origine: primo.quantitySource,
                confermato: primo.confirmed,
                annullaRimasto: state.azzeramentoUndo,
              };
            """,
        )

        self.assertEqual(esito["quantita"], 3)
        self.assertEqual(esito["origine"], "gestionale")
        self.assertTrue(esito["confermato"])
        self.assertIsNone(esito["annullaRimasto"])

    def test_la_fascia_di_annullo_dice_quante_ne_ha_azzerate(self) -> None:
        html = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              premi("reset-suggested-quantities");
              return renderQuantityStep();
            """,
        )

        self.assertIn('data-action="undo-azzeramento"', html)
        self.assertIn("1 quantità proposta dal gestionale azzerata", html)

    def test_l_etichetta_dice_che_cosa_tocca_e_che_cosa_no(self) -> None:
        html = self.esegui(
            "return renderToolbar();",
            preparazione=self.con_confronto(passo=2, revisione=self.con_due_origini()),
        )

        self.assertIn("Azzera le quantità proposte dal gestionale", html)
        self.assertIn("Le quantità che hai scritto tu restano.", html)
        self.assertNotIn("Azzera le quantità predefinite", html)

    def test_un_ricalcolo_butta_via_anche_questo_annullo(self) -> None:
        """The counterpart of the summary "×" test: the saved quantities
        belonged to the previous comparison."""

        rimasto = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              premi("reset-suggested-quantities");
              restoreExcludedProducts();
              return state.azzeramentoUndo;
            """,
        )

        self.assertIsNone(rimasto)

    def test_toccare_una_quantita_fa_scadere_la_fascia(self) -> None:
        """Pressing it after touching a number again would overwrite what
        the user just typed. `goToStep()` invalidates the supplier-move
        undo for the same reason."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              premi("reset-suggested-quantities");
              const prima = Boolean(state.azzeramentoUndo);
              premi("change-quantity", { productId: "p2", delta: "1" });
              return { prima, dopo: Boolean(state.azzeramentoUndo) };
            """,
        )

        self.assertTrue(esito["prima"])
        self.assertFalse(esito["dopo"])

    def test_senza_niente_da_azzerare_non_nasce_nessun_annullo(self) -> None:
        revisione = self.con_due_origini()
        revisione["products"][0]["quantitySource"] = "utente"
        annullo = self.esegui(
            self.con_confronto(passo=2, revisione=revisione) + """
              premi("reset-suggested-quantities");
              return state.azzeramentoUndo;
            """,
        )

        self.assertIsNone(annullo)


class AnnullaVuolDireUnaCosaSola(BancoDiProva):
    """"Annulla" (Cancel/Undo) must mean one thing, not two: "don't do what
    you were about to do" on delete confirmations and dialogs is opposite
    to "undo what I just did" on undo strips, so they can't share a label.
    The word stays for the first, the common meaning in any program; the
    second becomes "Rimetti" (Restore ...), which also says WHAT it restores.
    """

    def test_le_fasce_dicono_che_cosa_rimettono(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["quantitySource"] = "utente"
        revisione["products"].append({
            "id": "p2", "name": "Prodotto due", "quantity": 5, "quantitySource": "gestionale",
            "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
        })
        html = self.esegui(
            self.con_confronto(passo=2, revisione=revisione) + """
              premi("exclude-product", { productId: "p1" });
              premi("reset-suggested-quantities");
              return renderQuantityStep();
            """,
        )

        self.assertIn("Rimetti nell’ordine", html)
        self.assertIn("Rimetti le quantità", html)
        self.assertNotIn(">Annulla<", html)

    def test_le_conferme_di_eliminazione_dicono_ancora_annulla(self) -> None:
        """"Annulla" is correct here: it does nothing, leaving the document
        where it is."""

        html = self.esegui(
            self.con_confronto(passo=1) + """
              state.uploadDeletion.confirming = "gestionale.xlsx";
              return renderUploadStep();
            """,
        )

        self.assertIn('data-action="cancel-delete-upload"', html)
        self.assertIn(">Annulla<", html)

    def test_il_comando_che_esclude_si_legge_come_un_comando(self) -> None:
        """"Non ordinare" (don't order) read like an already-decided state,
        not a command to press, and the "✕" in front of it — the only cue
        that this was clickable — looked like the summary's "×", which does
        something else entirely. An imperative verb says it without glyphs,
        and "Escludi" (exclude) is the word the rest of the page already
        uses: the badge says "Escluso" and the filter says "Esclusi"."""

        html = self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2),
        )

        self.assertIn("Escludi dall’ordine", html)
        self.assertNotIn("Non ordinare", html)
        self.assertNotIn("✕", html)


class UnaFinestraCheSiApreSiPrendeIlFuoco(BancoDiProva):
    """`portaIlFuocoDentro` moves focus after a tick (`setTimeout(…, 0)`,
    because `autofocus` isn't honored on nodes inserted via `innerHTML`) and
    doesn't check where focus ended up in the meantime — a Tab pressed
    inside that tick would get pulled back. This is intentional: a dialog
    that opens must take focus, or a keyboard user is left outside whatever
    just appeared. The four call sites are all dialog openings (viewer,
    order column, catalog, supplier move), never a plain re-render.

    This test exists so that removing the mechanism, mistaking it for a
    bug, would break the four dialogs' accessibility instead of going unnoticed.
    """

    def test_il_fuoco_entra_nella_finestra_appena_si_apre(self) -> None:
        esito = self.esegui("""
          globalThis.fuochi = [];
          const dentro = document.querySelector('#catalog-search');
          dentro.focus = () => globalThis.fuochi.push('dentro-la-finestra');
          portaIlFuocoDentro(['#catalog-search']);
          await new Promise((r) => setTimeout(r, 0));
          return globalThis.fuochi;
        """, preparazione=self.con_confronto(passo=2))

        self.assertEqual(esito, ["dentro-la-finestra"])

    def test_se_il_primo_campo_non_c_e_si_ripiega_sul_secondo(self) -> None:
        """Calls always declare two selectors: the field, and the "×" that
        closes the dialog. A field that doesn't exist in that dialog — the
        catalog without a search box — must not leave focus outside."""

        esito = self.esegui("""
          globalThis.fuochi = [];
          dichiaraAssente('#catalog-search');
          const chiusura = document.querySelector('.catalog-dialog .dialog-close');
          chiusura.focus = () => globalThis.fuochi.push('la-x-che-chiude');
          portaIlFuocoDentro(['#catalog-search', '.catalog-dialog .dialog-close']);
          await new Promise((r) => setTimeout(r, 0));
          return globalThis.fuochi;
        """, preparazione=self.con_confronto(passo=2))

        self.assertEqual(esito, ["la-x-che-chiude"])

    def test_le_quattro_finestre_lo_chiedono_tutte(self) -> None:
        """If one lost this call, it would open a dialog the keyboard can't
        enter — unnoticed until someone who can't reach it with a mouse
        tries to use it."""

        sorgente = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        chiamate = sorgente.count("portaIlFuocoDentro([")

        self.assertEqual(chiamate, 4, "una delle quattro finestre non porta più il fuoco dentro")


class IlSiELIlNoSiDannoNelloStessoModo(BancoDiProva):
    """"Yes" and "no" must behave the same way: fire the request right
    away, await it, and report success or failure. Neither may act like
    the quantity fields' deferred autosave (450ms after the last edit, no
    declared wait), which would leave a checkmark drawn while the save is
    still pending, or after it failed. The correct form already existed in
    this card's twin, "È lo stesso prodotto? Sì / No".
    """

    def revisione(self) -> dict:
        base = json.loads(json.dumps(REVISIONE))
        prodotto = base["products"][0]
        prodotto["requiresConfirmation"] = True
        prodotto["confirmed"] = False
        prodotto["selectedSupplierId"] = "cipresso"
        return base

    def test_il_si_parte_subito_e_non_aspetta_il_salvataggio_differito(self) -> None:
        """Answering "yes" must fire the request right away and await it,
        not defer through the quantity fields' 450ms autosave path."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.revisione())
            + """
              globalThis.chiamate = [];
              const vero = globalThis.fetch;
              globalThis.fetch = (url, opzioni) => { globalThis.chiamate.push(String(url)); return vero(url, opzioni); };
              await premi("conferma-abbinamento", { productId: "p1", confermato: "true" });
              return { chiamate: globalThis.chiamate, confermato: state.review.products[0].confirmed };
            """,
            risposte={"/api/state": {"ok": True}},
        )

        self.assertTrue(esito["confermato"])
        self.assertIn("/api/state", esito["chiamate"])

    def test_se_il_salvataggio_non_riesce_il_si_torna_indietro(self) -> None:
        """A failed save must not leave the checkmark drawn: whoever sees it
        believes they've answered, and at compile time finds "Confirmation
        required · blocking" without understanding why."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.revisione())
            + """
              // ⚠ Il prodotto si tiene in mano prima: il gestore del click non
              // restituisce la promise — nessuno dei gestori di `app.js` lo fa
              // — quindi si lascia girare la coda per arrivare al ramo
              // d'errore, e in quei giri avanza anche la `loadReview()`
              // dell'avvio, che senza una risposta finta azzera `state.review`.
              const prodotto = state.review.products[0];
              premi("conferma-abbinamento", { productId: "p1", confermato: "true" });
              for (let giro = 0; giro < 8; giro += 1) await Promise.resolve();
              return { confermato: prodotto.confirmed, errore: state.runtimeError };
            """,
            # 500: the service exists and rejects the request; the checkmark
            # must not stay drawn over that failure.
            risposte={"/api/state": {"__stato": 500, "message": "Il servizio non risponde"}},
        )

        self.assertFalse(esito["confermato"], "il sì non deve restare disegnato")

    def test_mentre_il_si_va_i_due_pulsanti_dicono_che_stanno_lavorando(self) -> None:
        html = self.esegui(
            "state.matches.answering = 'p1:conferma'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2, revisione=self.revisione()),
        )

        self.assertIn("Attendere…", html)
        # And "no" is disabled: one answer at a time to the same question.
        self.assertIn("disabled", html.split('data-action="rifiuta-abbinamento"', 1)[1].split(">", 1)[0])

    def test_le_due_risposte_sono_due_pulsanti_gemelli(self) -> None:
        """Only one way to answer: no checkbox alongside the two buttons,
        and no leftover `data-product-confirm` marker."""

        html = self.esegui(
            "return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2, revisione=self.revisione()),
        )

        azioni = [pezzo.split('data-action="', 1)[1].split('"', 1)[0]
                  for pezzo in html.split("<button")[1:] if 'data-action="' in pezzo]
        self.assertEqual(azioni, ["conferma-abbinamento", "rifiuta-abbinamento"])
        self.assertNotIn("checkbox", html)
        self.assertNotIn("data-product-confirm", html)


class LeColonneSiRivedonoDallaScheda(BancoDiProva):
    """The command that opens the column selector, and the screen it opens.

    The manual column selector wasn't gone — it only opens when the
    pipeline stops on an unrecognized layout, and for a known supplier it
    never stops there, so there was no way in for a recognized document.

    The command lives inside the "Quali colonne leggo" panel, not among the
    document's other actions: that's already where the columns are being
    looked at, so a wrong-looking row gets corrected from where it was spotted.
    """

    COLONNE = """
      state.colonneDocumenti = {
        caricate: true, caricando: false, runId: "run-1", motivo: "", errore: "",
        perNome: {
          "LISTINO BETULLA.xlsx": {
            fileName: "LISTINO BETULLA.xlsx", role: "supplier", adapterId: "betulla_v1",
            sheet: "Sheet1", headerRow: 1, dataStartRow: 2, origin: "registro",
            orderColumn: { lettera: "C", colonna: 3, intestazione: "ORDINE", trovata: true },
            columns: [
              { campo: "unit_price_net", etichetta: "Prezzo netto", colonna: 6, lettera: "F",
                intestazione: "Cessione", esempio: "3,98", dichiarata: "Cessione", trovata: true }
            ]
          }
        }
      };
    """

    def scheda(self, extra: str = "") -> str:
        return self.esegui(
            'return renderFileCard({ id: "f1", name: "LISTINO BETULLA.xlsx", kind: "Listino", '
            'supplier: "BETULLA", supplierId: "betulla", status: "ready", schemaState: "SCHEMA_NOTO", '
            'rows: 10, message: "", deletable: true, uploadName: "LISTINO BETULLA.xlsx" });',
            preparazione=self.con_confronto() + self.COLONNE + extra,
        )

    def test_il_comando_sta_dove_si_guardano_le_colonne(self) -> None:
        html = self.scheda()

        self.assertIn('data-action="apri-colonne"', html)
        self.assertIn('data-upload-name="LISTINO BETULLA.xlsx"', html)
        self.assertIn("Rivedi le colonne", html)
        # Inside the panel, not among the document's actions: next to
        # "Elimina" it would sit among destructive commands.
        dentro = html.split('class="doc-columns"', 1)[1].split("</details>", 1)[0]
        self.assertIn('data-action="apri-colonne"', dentro)

    def test_e_il_comando_e_piccolo_e_discreto(self) -> None:
        """Small and discreet: it must not compete with "Continua"."""

        html = self.scheda()
        comando = html.split('data-action="apri-colonne"', 1)[0]

        self.assertIn("button--ghost", comando[-260:])
        self.assertIn("button--piccolo", comando[-260:])
        self.assertNotIn("button--primary", comando[-260:])

    def test_mentre_il_confronto_gira_il_comando_e_spento(self) -> None:
        """Changing the columns while the pipeline is reading that file makes no sense."""

        html = self.esegui(
            'return renderFileCard({ id: "f1", name: "LISTINO BETULLA.xlsx", kind: "Listino", '
            'supplier: "BETULLA", supplierId: "betulla", status: "ready", schemaState: "SCHEMA_NOTO", '
            'rows: 10, message: "", deletable: true, uploadName: "LISTINO BETULLA.xlsx" });',
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}) + self.COLONNE,
        )
        comando = html.split('data-action="apri-colonne"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", comando)

    DOCUMENTI = {
        "files": [
            {"name": "LISTINO BETULLA.xlsx", "kind": "Listino", "supplier": "BETULLA",
             "status": "ready", "deletable": True, "uploadName": "LISTINO BETULLA.xlsx"},
        ],
    }

    def pagina(self, extra: str = "") -> str:
        revisione = {**REVISIONE, **self.DOCUMENTI}
        return self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione) + extra,
        )

    def test_aperto_il_selettore_prende_la_pagina(self) -> None:
        """A preview table dropped in among the file cards would be unreadable."""

        html = self.pagina('state.schemaMapping.documento = "LISTINO BETULLA.xlsx";')

        self.assertIn("Colonne del listino", html)
        self.assertIn("LISTINO BETULLA.xlsx", html)
        self.assertIn('data-action="chiudi-colonne"', html)
        # And the document list is gone: one job at a time.
        self.assertNotIn('data-action="apri-colonne"', html)

    def test_e_dice_che_cosa_costa_cambiarle(self) -> None:
        """Changing a *read* column changes prices, so the comparison needs
        rerunning. A selector that doesn't say so implies saving is enough."""

        html = self.pagina('state.schemaMapping.documento = "LISTINO BETULLA.xlsx";')

        self.assertIn("cambia i prezzi del confronto", html)

    def test_i_comandi_del_selettore_non_fanno_ripartire_il_confronto(self) -> None:
        """A ten-minute comparison run isn't started without the user asking for it."""

        html = self.pagina(
            'state.schemaMapping.documento = "LISTINO BETULLA.xlsx";'
            'state.schemaMapping.data = { runId: "colonne-a-mano", suppliers: [], documents: ['
            '  { profileId: "p1", fileName: "LISTINO BETULLA.xlsx", format: "xlsx", sheets: [] } ] };'
            'state.schemaMapping.values = { p1: { role: "supplier", supplierChoice: "betulla", sheet: "",'
            '  headerRow: 1, dataStartRow: 2, columns: {} } };'
        )

        self.assertIn('data-action="salva-colonne"', html)
        self.assertIn("Salva le colonne", html)
        self.assertNotIn("riparti col confronto", html)
        self.assertIn("Le colonne nuove le usa il prossimo confronto", html)

    def test_la_fascia_dopo_il_salvataggio_non_dice_che_hai_caricato(self) -> None:
        """The document is the same file: what changes is how it's read."""

        frasi = self.esegui(
            "return frasiCambiamentoDocumenti();",
            preparazione=self.con_confronto(pipeline={
                "stato": "IN_ATTESA",
                "cambiamento": {"tipo": "colonne", "documenti": ["LISTINO BETULLA.xlsx"],
                                "fornitori": ["BETULLA"]},
            }),
        )

        self.assertEqual(
            frasi["titolo"],
            "Hai cambiato le colonne del listino BETULLA dopo l’ultimo confronto.",
        )

    def test_con_due_listini_la_frase_va_al_plurale(self) -> None:
        frasi = self.esegui(
            "return frasiCambiamentoDocumenti();",
            preparazione=self.con_confronto(pipeline={
                "stato": "IN_ATTESA",
                "cambiamento": {"tipo": "colonne", "documenti": [], "fornitori": ["BETULLA", "LARICE"]},
            }),
        )

        self.assertEqual(
            frasi["titolo"],
            "Hai cambiato le colonne dei listini BETULLA e LARICE dopo l’ultimo confronto.",
        )


class DoveFinisceIlProdottoCheHaiScartato(BancoDiProva):
    """Once "not the same item" is answered, the product leaves "Da
    confermare" — it isn't an open question anymore. The sentence telling
    the user where it went must point to the filter that actually contains
    it, not to "Nessuno ce l'ha" (nobody has it), which would be false: a
    price list did have a row for it, it was just rejected. Pointing to the
    wrong place is worse than saying nothing.
    """

    def prodotto_scartato(self) -> dict:
        return {"id": "p9", "name": "SAPONE", "quantity": 4, "selectedSupplierId": "",
                "offers": [{"supplierId": "cipresso", "available": False,
                            "rifiutata": {"since": "2026-08-22T10:00:00+00:00",
                                          "description": "SAPONE"}}]}

    def frase(self, prodotto: dict, *, rifiutata: bool = True) -> str:
        revisione = {**REVISIONE, "products": [prodotto]}
        return self.esegui(
            f'return doveFinisceIlProdotto("{prodotto["id"]}", {json.dumps(rifiutata)});',
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

    def test_manda_al_filtro_che_lo_contiene_davvero(self) -> None:
        frase = self.frase(self.prodotto_scartato())

        self.assertIn("Hai risposto no", frase)
        self.assertNotIn("Nessuno ce l’ha", frase)
        # And states the quantity is still there: the number needed to source it.
        self.assertIn("quantità", frase)

    def test_se_resta_un_fornitore_non_dice_niente(self) -> None:
        """A product that can still be ordered hasn't gone anywhere."""

        prodotto = self.prodotto_scartato()
        prodotto["offers"].append(
            {"supplierId": "betulla", "price": 10, "unitsPerOrderUnit": 6, "available": True},
        )

        self.assertEqual(self.frase(prodotto), "")

    def test_dopo_un_si_non_dice_niente(self) -> None:
        self.assertEqual(self.frase(self.prodotto_scartato(), rifiutata=False), "")


class IlPercheDelDocumentoInMappaturaGuidata(BancoDiProva):
    """The sentence at the top of a document in the guided column mapping.

    A supplier price list re-saved in Excel — losing a cell like the header
    that names the order column — can still reach this screen; labeling it
    "unknown document" invites configuring a known supplier as if it were
    new, overwriting the shipped adapter with a learned one. The adapter
    registry already knows whose document it is and what's missing from
    it, so the screen must say so instead of treating it as unknown.
    """

    def documento(self, motivo: dict) -> dict:
        return {"profileId": "p1", "fileName": "LISTINO BETULLA.xlsx", "reason": motivo}

    def test_al_documento_a_cui_manca_una_cella_si_dice_di_chi_e(self) -> None:
        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({
                "state": "QUASI", "supplierName": "BETULLA",
                "missing": [{"header": "ORDINE", "column": "C"}], "present": 4,
            })) + ");",
        )

        self.assertIn("BETULLA", html)
        self.assertIn("ORDINE", html)
        self.assertIn("colonna C", html)
        # The two things the reader needs: where the damage came from...
        self.assertIn("Excel", html)
        # ...and that configuring it here isn't the right move.
        self.assertIn("ricaricare l’originale", html)

    def test_senza_il_nome_del_fornitore_non_si_inventa_niente(self) -> None:
        """Better to keep the old sentence than show a half-built new one."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({
                "state": "QUASI", "supplierName": "",
                "missing": [{"header": "ORDINE", "column": "C"}], "present": 4,
            })) + ");",
        )

        self.assertEqual(html, "")

    def test_il_documento_sconosciuto_resta_senza_frase(self) -> None:
        """A document the registry genuinely doesn't recognize must not
        start looking like a known one."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({"state": "SCONOSCIUTO"})) + ");",
        )

        self.assertEqual(html, "")

    def test_il_listino_cambiato_dice_ancora_la_sua_frase(self) -> None:
        """The VARIATO sentence must not have been swallowed by the new one."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({
                "state": "VARIATO", "supplierName": "CIPRESSO",
                "changed": ["il nome del foglio"],
            })) + ");",
        )

        self.assertIn("CIPRESSO", html)
        self.assertIn("il nome del foglio", html)


class LaPaginaTreSiLeggeMentreLavora(BancoDiProva):
    """Three page-3 readability requests: none of them touch a number, all
    of them are about how page 3 reads and what's understood while the
    program is working.
    """

    DUE_FORNITORI = {
        **REVISIONE,
        "suppliers": [
            {"id": "cipresso", "name": "CIPRESSO", "minimumOrder": 0},
            {"id": "betulla", "name": "BETULLA", "minimumOrder": 0},
        ],
        "products": [
            {
                "id": "p1",
                "name": "Prodotto uno",
                "quantity": 3,
                "selectedSupplierId": "cipresso",
                "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
            },
            {
                "id": "p2",
                "name": "Prodotto due",
                "quantity": 2,
                "selectedSupplierId": "betulla",
                "offers": [{"supplierId": "betulla", "price": 4, "unitsPerOrderUnit": 3, "available": True}],
            },
        ],
    }

    def pagina(self, *, preparazione_extra: str = "") -> str:
        return self.esegui(
            f"{preparazione_extra} return renderCompileStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=self.DUE_FORNITORI, passo=3),
        )

    # --- Per-supplier list collapses ----------------------------

    def test_l_elenco_di_un_fornitore_nasce_aperto(self) -> None:
        """Starts open: collapsed by default would hide what was just opened to view."""

        html = self.pagina()

        self.assertIn('<details class="supplier-summary" data-ricorda="riepilogo-fornitore:cipresso" open', html)
        self.assertIn('<details class="supplier-summary" data-ricorda="riepilogo-fornitore:betulla" open', html)

    def test_il_nome_del_fornitore_e_il_comando_che_chiude(self) -> None:
        """The expected gesture: click the supplier name, not a separate arrow."""

        html = self.pagina()
        testata = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</summary>", 1)[0]

        self.assertIn('<summary class="supplier-summary__head">', testata)
        self.assertIn("CIPRESSO", testata)

    def test_chiuso_resta_visibile_il_subtotale(self) -> None:
        """Collapsing must keep the subtotal visible: it lives in the
        `<summary>`, the part that stays open, not in the product rows."""

        html = self.pagina()
        testata = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</summary>", 1)[0]
        corpo = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</details>", 1)[0]

        self.assertIn("supplier-summary__total", testata)
        self.assertNotIn("Prodotto uno", testata)
        self.assertIn("Prodotto uno", corpo)

    def test_chiuso_da_chi_ordina_non_si_riapre_al_ridisegno(self) -> None:
        """The regression this test guards against: the summary re-renders
        on every quantity change, so without remembering "closed" it would
        keep reopening under the user's fingers."""

        due = self.esegui(
            "const finto = { dataset: { ricorda: 'riepilogo-fornitore:cipresso' }, open: false };"
            "ricordaApertura(finto);"
            "return [renderCompileStep(), renderCompileStep()];",
            preparazione=self.con_confronto(pipeline=None, revisione=self.DUE_FORNITORI, passo=3),
        )

        for html in due:
            apertura = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split(">", 1)[0]
            self.assertNotIn("open", apertura)
            # The other supplier, untouched, stays open.
            self.assertIn("open", html.split('data-ricorda="riepilogo-fornitore:betulla"', 1)[1].split(">", 1)[0])

    def test_due_fornitori_non_si_chiudono_insieme(self) -> None:
        """The remember-key carries the supplier's id."""

        html = self.pagina()

        self.assertIn("riepilogo-fornitore:cipresso", html)
        self.assertIn("riepilogo-fornitore:betulla", html)

    def test_raggruppando_per_prodotto_non_si_chiude_niente(self) -> None:
        """This applies only to the per-supplier grouping: the same class,
        grouped by product, is a panel with nothing to collapse."""

        html = self.pagina(preparazione_extra='state.summary.grouping = "product";')

        self.assertIn('<article class="supplier-summary">', html)
        self.assertNotIn("riepilogo-fornitore:", html)

    # --- Per-supplier totals also on page 3 ----------------------

    def test_la_fascia_dei_totali_c_e_anche_in_pagina_tre(self) -> None:
        """The same component as on page 2, not a copy."""

        html = self.pagina()
        fascia = html.split('class="supplier-totals-strip"', 1)[1].split("</aside>", 1)[0]

        self.assertIn('aria-label="Totali ordine per fornitore"', html)
        self.assertIn("CIPRESSO", fascia)
        self.assertIn("BETULLA", fascia)
        self.assertIn("Totale", fascia)

    def test_la_fascia_sta_in_cima_prima_di_tutto_il_resto(self) -> None:
        """Pinned to the top and above the scrollable content: pinned at
        the bottom of the page would defeat the point."""

        html = self.pagina()

        self.assertLess(html.index("supplier-totals-strip"), html.index("Ordini da preparare"))
        self.assertLess(html.index("supplier-totals-strip"), html.index("Compilazione dei listini"))

    def test_la_fascia_e_l_unica_cosa_agganciata_in_alto_oltre_ai_passi(self) -> None:
        """Page 3 already has other pinned content at the top, and two
        elements pinned at the same offset would overlap: there must be
        only two distinct pinned offsets declared in the stylesheet."""

        foglio = STYLES.read_text(encoding="utf-8")
        regole = re.findall(r"([^{}]+)\{([^{}]*position:\s*sticky[^{}]*)\}", foglio)
        # A rule pinned at a nonzero offset sits BELOW something else: those
        # are the only ones that can overlap each other.
        def quota(corpo: str) -> str:
            trovata = re.search(r"top:\s*([^;]+);", corpo)
            return trovata.group(1).strip() if trovata else "0"

        sotto_qualcosa = {
            selettore.strip().splitlines()[-1].strip()
            for selettore, corpo in regole
            if quota(corpo) != "0"
        }

        self.assertEqual(sotto_qualcosa, {".supplier-totals-strip"})
        self.assertIn("top: calc(3rem + 4px)", foglio.split(".supplier-totals-strip {", 1)[1].split("}", 1)[0])
        # And what it's pinned below — the step bar — sits at zero.
        self.assertIn("top: 0", foglio.split(".stepper-shell {", 1)[1].split("}", 1)[0])

    # --- "Compila i listini" shows it's working ------------------

    def test_mentre_compila_si_vede_che_sta_lavorando(self) -> None:
        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertIn("compile-progress__bar", html)
        self.assertIn('role="progressbar"', html)
        self.assertIn("Sto scrivendo", html)

    def test_la_barra_non_c_e_quando_non_sta_compilando(self) -> None:
        """A bar sitting idle would be misleading, and after compilation
        finishes it would still claim to be working."""

        html = self.pagina()

        self.assertNotIn("compile-progress", html)

    def test_la_barra_non_promette_una_percentuale_che_non_ha(self) -> None:
        """The Node writer doesn't report progress fractions, so any number
        here would be made up. An absent `aria-valuenow` is the standard way
        to declare an indeterminate progress bar."""

        html = self.pagina(preparazione_extra="state.compiling = true;")
        barra = html.split("compile-progress__bar", 1)[1].split(">", 1)[0]

        self.assertNotIn("aria-valuenow", barra)
        self.assertNotIn("%", barra)
        self.assertNotIn("width:", barra)

    def test_la_barra_sta_sotto_il_pulsante_che_si_e_premuto(self) -> None:
        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertLess(html.index("Compilazione in corso…"), html.index("compile-progress"))

    def test_chi_ha_chiesto_meno_movimento_legge_la_frase(self) -> None:
        """Under `prefers-reduced-motion` the general rule zeroes all
        durations: a bar left in place would sit motionless, which is
        exactly what reads as "it crashed". The bar hides, the text stays."""

        foglio = STYLES.read_text(encoding="utf-8")
        ridotto = foglio.split("@media (prefers-reduced-motion: reduce) {")

        self.assertTrue(any(".compile-progress__bar" in blocco and "display: none" in blocco
                            for blocco in ridotto[1:]))

    def test_quante_copie_si_stanno_scrivendo(self) -> None:
        """The number comes from the real suppliers, and the plural is
        correct: `contati`, not a plain "s" tacked on."""

        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertIn("2 copie", html)


class IlPrezzoAlPezzoInPaginaTre(BancoDiProva):
    """Next to each summary row's total, in gray, the per-piece price under
    a heading that names it: the same number as page 2's "Prezzo per
    pezzo" column, the one the supplier was chosen with, and not part of
    any total.
    """

    # CIPRESSO: 10 € for a 6-piece carton (1.67 €/piece). BETULLA: 4 € for a 3-piece carton (1.33 €).
    REVISIONE = LaPaginaTreSiLeggeMentreLavora.DUE_FORNITORI

    def pagina(self, revisione: dict | None = None, *, extra: str = "") -> str:
        return self.esegui(
            f"{extra} return renderCompileStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione or self.REVISIONE, passo=3),
        )

    @staticmethod
    def riga(html: str, nome: str) -> str:
        return html.split(nome, 1)[1].split("</li>", 1)[0]

    def test_per_fornitore_il_prezzo_al_pezzo_sta_accanto_al_totale(self) -> None:
        html = self.pagina()

        self.assertRegex(self.riga(html, "Prodotto uno"),
                         r'<span class="order-lines__pezzo">1,67\xa0€<span class="order-lines__pezzo-etichetta"> al pezzo</span></span>\s*<strong>30,00\xa0€</strong>')
        self.assertRegex(self.riga(html, "Prodotto due"),
                         r'<span class="order-lines__pezzo">1,33\xa0€.*</span>\s*<strong>8,00\xa0€</strong>')

    def test_ogni_elenco_ha_l_intestazione_che_lo_spiega(self) -> None:
        html = self.pagina()

        # `aria-hidden`, like `.offer-grid__head`: a screen reader gets the
        # "per piece" label from the word next to each number instead.
        intestazione = ('<li class="order-lines__intestazione" aria-hidden="true"><span>Prodotto</span><span>Quantità</span>'
                        '<span></span><span>Prezzo al pezzo</span><span>Totale</span></li>')
        self.assertEqual(html.count(intestazione), 2, "una per fornitore")

    def test_per_prodotto_anche(self) -> None:
        html = self.pagina(extra='state.summary.grouping = "product";')

        self.assertIn("<span>Fornitore</span><span>Quantità</span><span></span><span>Prezzo al pezzo</span><span>Totale</span>", html)
        self.assertRegex(self.riga(html, "Prodotto uno"), r'1,67\xa0€.*\s*<strong>30,00\xa0€</strong>')
        self.assertRegex(self.riga(html, "Prodotto due"), r'1,33\xa0€.*\s*<strong>8,00\xa0€</strong>')

    def test_e_il_prezzo_del_listino_non_il_collo_diviso_i_pezzi(self) -> None:
        """16.74 / 12 = 1.3949999...; both page 2 and page 3 must show the
        price list's own `pricePerPiece` (1.395, which rounds to 1.40), not
        a value recomputed from raw division, which would round to 1.39."""

        revisione = {**self.REVISIONE, "products": [{
            "id": "p1", "name": "KIF CANDEGGINA SPRAY 650ML", "quantity": 2, "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "orderUnitPriceNet": 16.74, "quantityFactor": 12,
                        "pricePerPiece": 1.395, "available": True}],
        }]}

        pagina3 = self.pagina(revisione)
        pagina2 = self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione, passo=2),
        )

        self.assertIn('<span class="order-lines__pezzo">1,40\xa0€', pagina3)
        self.assertNotIn("1,39", pagina3)
        self.assertIn("1,40\xa0€ per pezzo", pagina2)
        self.assertNotIn("1,39", pagina2)

    def test_senza_fornitore_la_cella_resta_vuota(self) -> None:
        revisione = {**self.REVISIONE, "products": [*self.REVISIONE["products"], {
            "id": "p3", "name": "Prodotto tre", "quantity": 2,
            "offers": [{"supplierId": "betulla", "price": 0, "available": False}],
        }]}

        riga = self.riga(self.pagina(revisione, extra='state.summary.grouping = "product";'), "Prodotto tre")

        self.assertIn("Fornitore da scegliere", riga)
        self.assertIn('<span class="order-lines__pezzo"></span>', riga)
        self.assertNotIn("al pezzo", riga)

    def test_il_totale_non_cambia(self) -> None:
        """The per-piece price is purely informational: supplier and order
        totals are unchanged."""

        html = self.pagina()

        self.assertIn("<strong>30,00\xa0€</strong>", html)
        self.assertIn("<strong>8,00\xa0€</strong>", html)
        self.assertEqual(self.esegui("return allOrderTotal();", preparazione=self.con_confronto(
            pipeline=None, revisione=self.REVISIONE, passo=3)), 38)


class IniziaNuovaComparazione(BancoDiProva):
    """The command that opens a new weekly comparison.

    Before this, starting over meant manually removing the management-
    software export and then each price list one by one. Skipping that step
    is easy to miss: two price lists from the same supplier make only ONE
    enter the comparison, chosen by the file's modification date, silently
    dropping the other.
    """

    SENZA_CONFRONTO = {**REVISIONE, "files": [], "products": []}

    def pagina(self, *, revisione: dict | None = None, preparazione_extra: str = "") -> str:
        return self.esegui(
            f"{preparazione_extra} return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione or REVISIONE, passo=1),
        )

    # -- the command ----------------------------------------------------------

    def test_il_comando_sta_in_alto_nella_testata(self) -> None:
        """Not among the upload commands: it isn't a way to load a
        document, it's the gesture that opens the new week."""

        html = self.pagina()
        testata = html.split("page-heading__meta", 1)[1].split("</div>", 1)[0]

        self.assertIn('data-action="nuova-comparazione"', testata)
        self.assertIn("Inizia nuova comparazione", testata)
        # And it stays next to Settings, the other utility command.
        self.assertIn('data-action="apri-impostazioni"', testata)

    def test_senza_niente_da_svuotare_il_comando_e_spento(self) -> None:
        """A command that would do nothing must not look pressable on an
        empty state."""

        html = self.pagina(revisione=self.SENZA_CONFRONTO)
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", comando)

    def test_con_documenti_il_comando_e_premibile(self) -> None:
        html = self.pagina()
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertNotIn("disabled", comando)

    def test_mentre_il_confronto_gira_il_comando_e_spento(self) -> None:
        """The same rule as uploads: documents aren't changed under a
        pipeline that's reading them."""

        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(
                pipeline={"stato": "IN_CORSO", "fasi": []}, revisione=REVISIONE, passo=1,
            ),
        )
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", comando)

    # -- the confirmation dialog ----------------------------------------------

    def test_prima_di_chiedere_non_si_vede_nessuna_conferma(self) -> None:
        html = self.pagina()

        self.assertNotIn("conferma-riga", html)

    def test_la_conferma_dice_quanti_documenti_toglie(self) -> None:
        """The number matches the cards visible above; verified by eye. It's
        not counted by `role`, which `normalizeReview` doesn't copy — a
        role-based count came out zero even with four documents on the page."""

        html = self.pagina(preparazione_extra="state.nuovaComparazione.chiedendo = true;")
        riga = html.split("conferma-riga", 1)[1].split("</div>", 1)[0]

        self.assertIn("2 documenti caricati", riga)

    def test_la_conferma_dice_che_cosa_resta(self) -> None:
        """The half that matters: the reader's worry isn't what the command
        removes, it's not knowing whether it removes something they need."""

        html = self.pagina(preparazione_extra="state.nuovaComparazione.chiedendo = true;")
        riga = html.split("conferma-riga", 1)[1].split("</div>", 1)[0]

        self.assertIn("I file sul tuo computer non si toccano", riga)
        self.assertIn("le conferme che hai dato", riga)
        self.assertIn("gli ordini di cui aspetti la merce restano", riga)

    def test_la_conferma_ha_una_risposta_e_una_via_d_uscita(self) -> None:
        html = self.pagina(preparazione_extra="state.nuovaComparazione.chiedendo = true;")

        self.assertIn('data-action="conferma-nuova-comparazione"', html)
        self.assertIn('data-action="annulla-nuova-comparazione"', html)
        self.assertIn("Sì, comincia", html)

    def test_mentre_svuota_non_si_preme_due_volte(self) -> None:
        html = self.pagina(preparazione_extra="state.nuovaComparazione.chiedendo = true; state.nuovaComparazione.inCorso = true;")
        conferma = html.split('data-action="conferma-nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", conferma)
        self.assertIn("Attendere…", html)

    def test_il_gesto_apre_e_chiude_la_conferma(self) -> None:
        """The handler's two branches, exercised by pressing for real."""

        esito = self.esegui(
            'premi("nuova-comparazione");'
            "const aperta = state.nuovaComparazione.chiedendo;"
            'premi("annulla-nuova-comparazione");'
            "return { aperta, chiusa: state.nuovaComparazione.chiedendo };",
            preparazione=self.con_confronto(pipeline=None, revisione=REVISIONE, passo=1),
        )

        self.assertTrue(esito["aperta"])
        self.assertFalse(esito["chiusa"])

    # -- last week's pending-order question ----------------------

    def test_senza_confronto_la_domanda_sugli_ordini_sta_in_pagina_1(self) -> None:
        """The moment to ask "did it arrive?" is when starting a new
        comparison, not halfway through page 2."""

        html = self.esegui(
            'state.history.pending = [{ orderId: "o1", supplier: "betulla", supplierName: "BETULLA",'
            ' createdAt: "2026-08-17T10:00:00+02:00", lineCount: 12, totalNet: 120 }];'
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=self.SENZA_CONFRONTO, passo=1),
        )

        self.assertIn("pending-orders", html)
        self.assertIn("Hai ricevuto la merce ordinata da BETULLA", html)
        self.assertIn('data-action="history-received"', html)

    def test_col_confronto_pieno_la_domanda_resta_dov_era(self) -> None:
        """With a comparison loaded, page 1 is the documents page: the
        question doesn't intrude there and stays on page 2 as usual."""

        html = self.esegui(
            'state.history.pending = [{ orderId: "o1", supplier: "betulla", supplierName: "BETULLA",'
            ' createdAt: "2026-08-17T10:00:00+02:00", lineCount: 12, totalNet: 120 }];'
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=REVISIONE, passo=1),
        )

        self.assertNotIn("pending-orders", html)



class RimetteNellOrdineRimetteLaQuantita(BancoDiProva):
    """Restoring an excluded product must restore its quantity, supplier and
    confirmation, not just clear the excluded flag, through either path:
    the "... è stato escluso. [Rimetti nell'ordine]" strip, and the button
    on the product's own card — the only one left once the strip expires,
    or after a page reload. Clearing only the flag would put the product
    back in the list without it being ordered.
    """

    def esclude_e_rimette(self, in_mezzo: str = "") -> dict:
        return self.esegui(
            self.con_confronto(passo=2) + f"""
              premi("exclude-product", {{ productId: "p1" }});
              {in_mezzo}
              premi("restore-product", {{ productId: "p1" }});
              const prodotto = findProduct("p1");
              return {{
                quantita: orderQuantity(prodotto),
                fornitore: prodotto.selectedSupplierId,
                escluso: isExcluded(prodotto),
              }};
            """,
        )

    def test_la_quantita_torna_quella_di_prima(self) -> None:
        esito = self.esclude_e_rimette()
        self.assertEqual(esito["quantita"], 3)
        self.assertEqual(esito["fornitore"], "cipresso")
        self.assertFalse(esito["escluso"])

    def test_torna_anche_quando_la_fascia_e_gia_scaduta(self) -> None:
        """There's only ONE strip: excluding a second product replaces the undo for the first."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.revisione_con_due_prodotti()) + """
              premi("exclude-product", { productId: "p1" });
              premi("exclude-product", { productId: "p2" });
              premi("restore-product", { productId: "p1" });
              const prodotto = findProduct("p1");
              return { quantita: orderQuantity(prodotto), annullo: state.exclusionUndo && state.exclusionUndo.id };
            """,
        )
        # The strip's undo refers to the second product…
        self.assertEqual(esito["annullo"], "p2")
        # …and the first is restored the same way, with its own quantity.
        self.assertEqual(esito["quantita"], 3)

    def test_torna_anche_dopo_un_ricaricamento_della_pagina(self) -> None:
        """The real scenario: exclude today, change your mind tomorrow morning.

        The reload is staged the way the program does it: the comparison is
        re-fetched from the service — where the excluded product has
        quantity zero, since that's how the service persists it — and
        excluded products are re-read from browser storage.
        """

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              premi("exclude-product", { productId: "p1" });
              const scritto = localStorage.getItem("confronto-fornitori:esclusi:R1");
              // Il ricaricamento: confronto nuovo di zecca, con la quantità già
              // azzerata dal servizio, e gli esclusi riletti dal browser.
              const daCapo = JSON.parse(JSON.stringify(state.review.raw || null)) ;
              state.review = normalizeReview({
                run: { id: "R1", status: "ready", createdAt: "2026-08-10T09:00:00+02:00", label: "Confronto" },
                files: [], suppliers: [{ id: "cipresso", name: "CIPRESSO", minimumOrder: 500 }],
                products: [{ id: "p1", name: "Prodotto uno", quantity: 0, excluded: true,
                             selectedSupplierId: "",
                             offers: [{ supplierId: "cipresso", price: 10, unitsPerOrderUnit: 6, available: true }] }],
              });
              restoreExcludedProducts();
              premi("restore-product", { productId: "p1" });
              const prodotto = findProduct("p1");
              return { scritto, quantita: orderQuantity(prodotto), fornitore: prodotto.selectedSupplierId };
            """,
        )
        self.assertIn("p1", esito["scritto"])
        self.assertEqual(esito["quantita"], 3)
        self.assertEqual(esito["fornitore"], "cipresso")

    def test_un_elenco_vecchio_nella_memoria_del_browser_non_rompe_niente(self) -> None:
        """A user who reloads with products excluded under the old storage
        format still has that old shape: a plain list of ids, with no
        record of their previous quantity. "Rimetti nell'ordine" falls back
        to its old behavior — restoring the product and leaving the
        quantity as is — instead of breaking.
        """

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              localStorage.setItem("confronto-fornitori:esclusi:R1", JSON.stringify(["p1"]));
              restoreExcludedProducts();
              const escluso = isExcluded(findProduct("p1"));
              premi("restore-product", { productId: "p1" });
              return { escluso, dopo: isExcluded(findProduct("p1")) };
            """,
        )
        self.assertTrue(esito["escluso"])
        self.assertFalse(esito["dopo"])

    def revisione_con_due_prodotti(self) -> dict:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"].append({
            "id": "p2",
            "name": "Prodotto due",
            "quantity": 5,
            "selectedSupplierId": "cipresso",
            "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}],
        })
        return revisione



class ILavoriNonSiSovrappongono(BancoDiProva):
    """Four ways of pressing the right command at the wrong time, all
    sharing the same shape: the page allows an action that can't safely
    happen right now, and the damage only shows up later — a comparison run
    on last week's price lists, an order compiled with stale prices, a
    misleading error, a disappearing link.
    """

    def test_confronta_e_spento_se_ci_sono_documenti_scelti_e_non_caricati(self) -> None:
        """The trap: a new price list is selected but not yet uploaded."""

        comando = self.esegui("return comandoConfronto();",
                              preparazione=self.con_confronto(pendenti=1))
        self.assertTrue(comando["disabilitato"])
        self.assertIn("caricali con il comando qui sopra", comando["nota"].lower())

    def test_senza_documenti_scelti_il_comando_torna(self) -> None:
        """Counterpart: the button isn't disabled forever."""

        comando = self.esegui("return comandoConfronto();",
                              preparazione=self.con_confronto(pendenti=0))
        self.assertFalse(comando["disabilitato"])

    def test_non_si_compila_mentre_il_confronto_si_aggiorna(self) -> None:
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3, pipeline={"stato": "IN_CORSO", "messaggio": "Lettura…", "fasi": []}),
        )
        self.assertIn("Il confronto si sta aggiornando…", html)
        self.assertIn("i listini nascerebbero con i prezzi di prima", html)

    def test_finito_il_ricalcolo_il_comando_torna(self) -> None:
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3, pipeline={"stato": "COMPLETATO", "messaggio": "Fatto.", "fasi": []}),
        )
        self.assertIn("Compila i listini", html)
        self.assertNotIn("Il confronto si sta aggiornando…", html)

    def test_il_doppio_clic_manda_una_richiesta_sola(self) -> None:
        """A double-click on this button is the most natural gesture there
        is: nothing happens for a second, so it gets pressed again. Without
        a guard, both clicks fire the request — the service accepts the
        first and answers 409 to the second — and the page reports "the
        comparison didn't start" while it actually had.
        """

        esito = self.esegui(
            self.con_confronto(pipeline={"stato": "IN_ATTESA"}) + """
              return (async () => {
                const primo = avviaRicalcolo();
                const secondo = avviaRicalcolo();
                await primo; await secondo;
                return {
                  richieste: chiamate.filter((voce) => voce.indirizzo.includes("/api/pipeline/avvia")).length,
                  errore: state.pipeline.errore,
                };
              })();
            """,
            risposte={"/api/pipeline/avvia": {"stato": "IN_CORSO", "messaggio": "Confronto avviato.", "fasi": []}},
        )
        self.assertEqual(esito["richieste"], 1)
        self.assertEqual(esito["errore"], "")

    def test_premere_il_passo_su_cui_si_e_gia_non_cancella_l_esito(self) -> None:
        """Re-pressing the step you're already on must not clear the
        "download the price lists" link, or it would vanish the moment the
        compilation finishes."""

        esito = self.esegui(
            self.con_confronto(passo=3) + """
              state.compileResult = { ok: true, cartella: "2026-08-22_1830" };
              premiPasso(3);
              const dopoLoStesso = state.compileResult && state.compileResult.cartella;
              premiPasso(1);
              return { dopoLoStesso, dopoUnAltro: state.compileResult };
            """,
        )
        self.assertEqual(esito["dopoLoStesso"], "2026-08-22_1830")
        # Changing step does clear it, as before: that result belongs to
        # page 3, and returning to it reloads the compilation history.
        self.assertIsNone(esito["dopoUnAltro"])



class LeRisposteVecchieNonVincono(BancoDiProva):
    """With two requests in flight, a stale response must not overwrite
    state written by a more recent one, regardless of arrival order.

    This happens when typing in the price-list viewer's search box while an
    earlier search is still loading, and when opening "Cambia colonna" for
    one supplier, closing it and opening it for another. The consequence
    isn't cosmetic: the viewer's table can write a permanent match to
    `conferme.db`, and the column dialog sends a supplier and a column
    number to the service.
    """

    def test_una_lettura_del_listino_superata_non_scrive_le_sue_righe(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=2) + """
              state.listino.fornitore = "cipresso";
              return (async () => {
                const inVolo = caricaIlListino();
                // Nel frattempo ne parte un'altra: la ricerca è cambiata.
                state.listino.richiesta += 1;
                await inVolo;
                return { righe: state.listino.righe.length, caricando: state.listino.caricando };
              })();
            """,
            risposte={"/api/listino": {"supplier": "cipresso", "righe": [{"riga": 1}, {"riga": 2}], "totale": 2}},
        )
        self.assertEqual(esito["righe"], 0)
        # It doesn't clear the loading flag either: that's for the real
        # (newer) request to clear.
        self.assertTrue(esito["caricando"])

    def test_la_lettura_piu_recente_invece_scrive(self) -> None:
        """Counterpart: it's not that nothing ever applies anymore."""

        esito = self.esegui(
            self.con_confronto(passo=2) + """
              state.listino.fornitore = "cipresso";
              return (async () => {
                await caricaIlListino();
                return { righe: state.listino.righe.length, caricando: state.listino.caricando };
              })();
            """,
            risposte={"/api/listino": {"supplier": "cipresso", "righe": [{"riga": 1}, {"riga": 2}], "totale": 2}},
        )
        self.assertEqual(esito["righe"], 2)
        self.assertFalse(esito["caricando"])

    def test_le_colonne_di_un_fornitore_non_finiscono_in_un_altro(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=1) + """
              return (async () => {
                const inVolo = apriLaColonnaDOrdine("betulla");
                // La finestra viene chiusa e riaperta su un altro fornitore.
                state.colonnaOrdine.fornitore = "larice";
                await inVolo;
                return { fornitore: state.colonnaOrdine.fornitore, colonne: state.colonnaOrdine.colonne.length };
              })();
            """,
            risposte={"/api/schemas/order-column": {
                "colonne": [{"colonna": "C", "scegliibile": True}, {"colonna": "D", "scegliibile": True}],
                "attuale": {"colonna": "C"}, "supplierName": "BETULLA",
            }},
        )
        self.assertEqual(esito["fornitore"], "larice")
        self.assertEqual(esito["colonne"], 0)

    def test_le_colonne_del_fornitore_giusto_arrivano(self) -> None:
        esito = self.esegui(
            self.con_confronto(passo=1) + """
              return (async () => {
                await apriLaColonnaDOrdine("betulla");
                return { fornitore: state.colonnaOrdine.fornitore, colonne: state.colonnaOrdine.colonne.length };
              })();
            """,
            risposte={"/api/schemas/order-column": {
                "colonne": [{"colonna": "C", "scegliibile": True}, {"colonna": "D", "scegliibile": True}],
                "attuale": {"colonna": "C"}, "supplierName": "BETULLA",
            }},
        )
        self.assertEqual(esito["fornitore"], "betulla")
        self.assertEqual(esito["colonne"], 2)



class UscireDalleColonneNonButtaViaIlLavoro(BancoDiProva):
    """"Torna ai documenti" (back to documents) must not silently discard
    everything set up inside "Rivedi le colonne" — sheet, header row, data
    start row and a column per field, up to ten fields checked against the
    file — since its label only promises to go back, not to discard.
    """

    def con_colonne_aperte(self, cambiando: str = "") -> str:
        return self.con_confronto(passo=1) + f"""
          initializeSchemaMapping({{
            runId: "r1",
            documents: [{{
              profileId: "p1", fileName: "listino.xlsx",
              sheets: [{{ name: "Foglio1" }}],
              suggestion: {{ role: "supplier", supplierId: "betulla", sheet: "Foglio1", headerRow: 1, columns: {{}} }},
            }}],
          }});
          state.schemaMapping.documento = "listino.xlsx";
          {cambiando}
        """

    def test_senza_modifiche_si_esce_e_basta(self) -> None:
        esito = self.esegui(
            self.con_colonne_aperte() + """
              premi("chiudi-colonne");
              return { aperta: Boolean(state.schemaMapping.data), chiede: state.schemaMapping.chiedendoUscita };
            """,
        )
        self.assertFalse(esito["aperta"])
        self.assertFalse(esito["chiede"])

    def test_con_modifiche_chiede_prima(self) -> None:
        esito = self.esegui(
            self.con_colonne_aperte('state.schemaMapping.values["p1"].sheet = "Foglio2";') + """
              premi("chiudi-colonne");
              return { aperta: Boolean(state.schemaMapping.data), chiede: state.schemaMapping.chiedendoUscita };
            """,
        )
        self.assertTrue(esito["aperta"], "le colonne impostate sono sparite senza chiedere")
        self.assertTrue(esito["chiede"])

    def test_la_domanda_dice_che_cosa_si_perde(self) -> None:
        html = self.esegui(
            self.con_colonne_aperte('state.schemaMapping.values["p1"].sheet = "Foglio2";') + """
              premi("chiudi-colonne");
              return renderColonneDocumento();
            """,
        )
        self.assertIn("Esci senza salvare le colonne?", html)
        self.assertIn("non è ancora stato salvato e va perso", html)
        self.assertIn('data-action="esci-dalle-colonne"', html)
        self.assertIn('data-action="resta-nelle-colonne"', html)

    def test_si_puo_restare(self) -> None:
        esito = self.esegui(
            self.con_colonne_aperte('state.schemaMapping.values["p1"].sheet = "Foglio2";') + """
              premi("chiudi-colonne");
              premi("resta-nelle-colonne");
              return {
                aperta: Boolean(state.schemaMapping.data),
                chiede: state.schemaMapping.chiedendoUscita,
                foglio: state.schemaMapping.values.p1.sheet,
              };
            """,
        )
        self.assertTrue(esito["aperta"])
        self.assertFalse(esito["chiede"])
        self.assertEqual(esito["foglio"], "Foglio2")

    def test_e_si_puo_uscire_lo_stesso(self) -> None:
        esito = self.esegui(
            self.con_colonne_aperte('state.schemaMapping.values["p1"].sheet = "Foglio2";') + """
              premi("chiudi-colonne");
              premi("esci-dalle-colonne");
              return { aperta: Boolean(state.schemaMapping.data), chiede: state.schemaMapping.chiedendoUscita };
            """,
        )
        self.assertFalse(esito["aperta"])
        self.assertFalse(esito["chiede"])



class IlComandoDellaColonnaDOrdineCompareDavvero(BancoDiProva):
    """`renderColonnaDellOrdine` only draws the "Cambia colonna" button when
    `file.supplierId` is set, so `normalizeReview` must copy that field
    through: dropping it silently hides the button even though the service
    sent it and the row above still says which column the order goes in.

    A test that hand-builds `renderColonnaDellOrdine({supplierId: "betulla"},
    …)` only exercises the template, not this data path — these tests start
    from `normalizeReview`, like the page does.
    """

    def con_documento(self) -> str:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["files"] = [{
            "name": "LISTINO BETULLA.xlsx", "kind": "Listino", "supplier": "BETULLA",
            "supplierId": "betulla", "role": "supplier", "status": "ready", "deletable": True,
        }]
        return self.con_confronto(passo=1, revisione=revisione) + """
          state.colonneDocumenti = {
            caricate: true, caricando: false, runId: "", motivo: "", errore: "",
            perNome: { "LISTINO BETULLA.xlsx": {
              sheet: "Listino", headerRow: 1, dataStartRow: 2, origin: "registro",
              columns: [{ lettera: "A", intestazione: "EAN", etichetta: "Codice a barre" }],
              orderColumn: { lettera: "C", intestazione: "ORDINE" },
            } },
          };
        """

    def test_il_pulsante_c_e_sulla_scheda_del_documento(self) -> None:
        html = self.esegui("return renderFileCard(state.review.files[0]);",
                           preparazione=self.con_documento())

        self.assertIn("colonna <strong>C</strong>", html)
        self.assertIn('data-action="apri-colonna-ordine"', html)
        self.assertIn('data-supplier-id="betulla"', html)

    def test_il_fornitore_sopravvive_alla_normalizzazione(self) -> None:
        """The missing field, tested in isolation: without it, the button never renders."""

        fornitore = self.esegui("return state.review.files[0].supplierId;",
                                preparazione=self.con_documento())

        self.assertEqual(fornitore, "betulla")

    def test_sul_gestionale_non_c_e_niente_da_spostare(self) -> None:
        """Counterpart: the management-software export has no order column."""

        revisione = json.loads(json.dumps(REVISIONE))
        revisione["files"] = [{
            "name": "gestionale.xlsx", "kind": "Gestionale", "supplier": "Gestionale",
            "role": "master", "status": "ready", "deletable": True,
        }]
        html = self.esegui(
            "return renderFileCard(state.review.files[0]);",
            preparazione=self.con_confronto(passo=1, revisione=revisione) + """
              state.colonneDocumenti = {
                caricate: true, caricando: false, runId: "", motivo: "", errore: "",
                perNome: { "gestionale.xlsx": { sheet: "Foglio1", headerRow: 2, dataStartRow: 3,
                  origin: "registro", columns: [], orderColumn: null } },
              };
            """,
        )

        self.assertNotIn('data-action="apri-colonna-ordine"', html)


if __name__ == "__main__":
    unittest.main()
