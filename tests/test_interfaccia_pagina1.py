"""La pagina 1 come la vede chi la usa: si esegue davvero, non si cerca a mano.

Perche' cosi'.  I test d'interfaccia gia' presenti nel progetto leggono
`app.js` come testo e cercano sottostringhe.  E' precisamente il difetto che ha
permesso a tre correzioni di essere annullabili a suite verde: una sottostringa
generica sopravvive alla mutazione che toglie il comportamento.  Qui `app.js`
viene **caricato ed eseguito** in Node — lo stesso Node che il writer usa per
scrivere i listini, cercato con `find_node_runtime()` — dentro un DOM finto, e
si guarda che cosa restituiscono le funzioni di render.

Quello che questo file NON copre, e va detto:

* niente browser: non ci sono layout, CSS applicato, eventi di mouse o
  tastiera.  Le classi CSS si controllano solo per esistenza nel foglio di
  stile, non per effetto;
* i gestori di click adesso si PREMONO: il nodo finto li registra e
  `premi("azione", {dati})` li esegue davvero.  Non era cosi' fino al 19 agosto
  2026, e finche' non lo e' stato si poteva mettere `return` in testa a tutti e
  due i gestori unici di `app.js` e restavano verdi 295 collaudi con nessun
  pulsante funzionante.  Resta vero che non ci sono eventi veri di mouse e
  tastiera: `premi` costruisce l'evento, non lo fa nascere il browser;
* il servizio locale e' finto (`fetch` risponde da una tabella): si prova come
  la pagina *reagisce* a una risposta, non che il servizio la mandi davvero.

Se Node non c'e', i test che lo richiedono si saltano e restano quelli sul
sorgente, che sono dichiarati come tali nei rispettivi docstring.
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

# `app/` e `scripts/` sul percorso, come fanno gli altri file di prova. Senza,
# `percorso_node()` non riesce a caricare `launcher.py` — che importa
# `versione_del_codice` — l'eccezione finisce nel suo `except` e Node risulta
# assente: 201 prove su 206 si saltano in silenzio. Nella suite intera non si
# vedeva, perche' un file caricato prima aveva gia' preparato il percorso.
for _cartella in (ROOT / "app", ROOT / "scripts"):
    if str(_cartella) not in sys.path:
        sys.path.insert(0, str(_cartella))


def percorso_node() -> str | None:
    """Il Node del progetto, cercato come lo cerca il writer."""

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
    except Exception:  # noqa: BLE001 - senza Node si salta, non si fallisce
        return None
    return str(runtime.executable) if runtime else None


# Il banco di prova: un DOM finto quanto basta perche' `app.js` si carichi, e
# un `fetch` che risponde da una tabella indirizzo -> corpo.  Le funzioni di
# `app.js` restano quelle vere: qui non si riscrive niente.
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


# Un confronto ordinario: elenco del gestionale, un listino, un prodotto con
# un'offerta.  E' la base su cui le prove cambiano una cosa sola alla volta.
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
    """Base comune: esegue `app.js` vero dentro Node."""

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
        """Prepara `state` con un confronto già fatto e uno stato della catena."""

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
    """Correzione 1 — la frase «I documenti sono cambiati» non si butta piu' via.

    Prove eseguite: si chiamano le funzioni vere di `app.js`.
    """

    def test_in_attesa_senza_cambiamento_non_disegna_niente(self) -> None:
        """L'assunto originale resta giusto: senza `cambiamento` non è partito niente."""

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
        # Il nome vero del fornitore, non «un listino» e non il nome del file.
        self.assertIn("Hai eliminato il listino CIPRESSO dopo l’ultimo confronto.", html)
        # E la data del confronto che si sta ancora guardando.
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
        """«Non deve impedire niente»: il pulsante Continua resta premibile."""

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
        """Il pezzo che il codice d'avvio buttava via.

        Qui non si prepara niente a mano: si lascia partire `loadReview()` e la
        richiesta dello stato della catena esattamente come all'apertura del
        programma, con il servizio locale che risponde IN_ATTESA + cambiamento.
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
        # E senza barra delle fasi: non c'e' nessun ricalcolo da seguire.
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
        # Li' «pagine 2 e 3» non vuol dire niente: si parla di quello che si vede.
        self.assertIn("I prezzi che vedi qui sotto sono ancora quelli di lunedì 10 agosto.", html)
        self.assertIn('data-action="vai-al-ricalcolo"', html)

    def test_un_cambiamento_malformato_non_rompe_la_pagina(self) -> None:
        """Il campo può non esserci o arrivare sbagliato: si fa come prima."""

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
    """Correzione 2 — la pagina 1 dice a chi tocca.

    Prove eseguite.
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
        """Con file in attesa E la catena che gira, il caricamento e' spento:
        offrire «Carica i documenti scelti» come primario sarebbe una presa in giro."""

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
        """«Ricalcola» diventa «Confronta i listini» quando non c'è un confronto."""

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
        """Con un confronto vecchio non promette i prezzi di adesso."""

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
        """La regola, misurata sull'HTML vero della pagina."""

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
                # I primari spenti non contano: un pulsante disabilitato non
                # chiede di essere premuto.
                attivi = [
                    pezzo for pezzo in html.split("<button")
                    if "button--primary" in pezzo.split(">")[0] and "disabled" not in pezzo.split(">")[0]
                ]
                self.assertLessEqual(len(attivi), 1, f"{nome}: {[p.split('>')[0] for p in attivi]}")

    def test_nella_pagina_1_non_compare_gergo(self) -> None:
        """Non nel sorgente: nell'HTML che la pagina produce davvero."""

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
        """`conferma_schemi` rifà `valida_mappature` da solo: la prova è facoltativa."""

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

    # -- «non lo conosco» e «lo conosco ed è cambiato» sono due notizie ------
    #
    # Il 21 agosto 2026 erano la stessa frase. Un utente ha caricato UN listino
    # nuovo e si è visto aprire la procedura guidata anche sul listino di
    # CIPRESSO — che il registro riconosce con confidenza 0,98, colonne tutte al
    # loro posto — con scritto sopra «Non riconosco le colonne di 2 documenti».

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
        # E la spiegazione sta sulla scheda del cambiato, non su quella del nuovo.
        prima, dopo = html.split("3listino_Cipresso.xlsx", 1)
        self.assertNotIn("lo conosco", prima)
        self.assertIn("lo conosco", dopo)

    def test_senza_proposta_la_tendina_non_sceglie_per_te(self) -> None:
        """Preselezionare qualcosa vuol dire che Conferma senza guardare fa qualcosa."""

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
        """Oggi si scopriva dopo: il file più vecchio esce dal confronto in silenzio."""

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
        """Prima l'errore sostituiva tutto il modulo, e non restava niente da correggere."""

        preparazione = self.con_documenti(self.documento("d1", "OFFERTE.xlsx", "SCONOSCIUTO")) + """
          state.schemaMapping.error = "OFFERTE.xlsx: completa prezzo, pezzi per collo.";
        """
        html = self.esegui("return renderSchemaMappingWizard();", preparazione=preparazione)

        self.assertIn("completa prezzo, pezzi per collo", html)
        self.assertIn("data-schema-document", html)
        self.assertIn("confirm-schemas", html)


class TestiInItaliano(BancoDiProva):
    """Correzione 3 — i testi riscritti, letti dall'HTML che la pagina produce.

    Prove eseguite.
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
        # Il prodotto vale 30 € contro un minimo di 500: l'avviso c'e' di sicuro.
        self.assertIn("al minimo d’ordine di CIPRESSO", html)
        for gergo in ("soglia netta", "minimo netto", "sotto soglia"):
            self.assertNotIn(gergo, html)

    def test_la_conferma_dice_subito_perche_serve(self) -> None:
        revisione = json.loads(json.dumps(REVISIONE))
        revisione["products"][0]["requiresConfirmation"] = True
        preparazione = self.con_confronto(pipeline=None, passo=2, revisione=revisione)
        html = self.esegui("return renderConfirmation(state.review.products[0]);", preparazione=preparazione)
        # ⚠ Una domanda, e si vede che è una domanda: dal 22 agosto 2026 le due
        # risposte sono due pulsanti gemelli, non una casella e un pulsante.
        self.assertIn("È lo stesso articolo?", html)
        self.assertIn("Sì, è lo stesso", html)
        self.assertIn("No, non è lo stesso", html)
        self.assertIn("Il codice a barre non coincide: confronta nome e formato con quello qui sotto.", html)
        # Il motivo non sta piu' dietro un pieghevole.
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
        """Il servizio manda etichette da programma; la pagina non le ricopia.

        Da quando ogni riga scartata viene contata col suo motivo, in questo
        riquadro arrivano `senza_prezzo` e `DISPLAY_COMPONENT`: scritti cosi'
        sono il nome di una variabile, non una spiegazione.
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
        # 4 + 2 non ordinabili, 3 escluse: il totale si legge in cima.
        self.assertIn("9 righe scartate", html)

    def test_un_motivo_che_la_pagina_non_conosce_si_mostra_com_e(self) -> None:
        """Le etichette dello scarto le scrive chi configura il listino.

        Inventarne una traduzione le nasconderebbe: meglio una parola tecnica
        che niente.
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
        # Il testo tecnico non sparisce: si sposta nel pieghevole.
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
    """Correzione 4 — i quattro rilievi. Prove eseguite."""

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
        # E si torna indietro: stesso comando della domanda, risposta opposta.
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
        """Il fornitore salvato non ha più offerta: si riassegna, ma si dice."""

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
        # E non si presenta come pienamente riuscita.
        self.assertIn("results--partial", html)
        self.assertIn("non tutto è andato a buon fine", html)

    def test_una_compilazione_riuscita_e_pulita_non_si_presenta_come_a_meta(self) -> None:
        """⚠ Qui c'era `test_un_fornitore_senza_copia_si_dice_per_nome`.

        Quel test fabbricava una risposta con `errors: [{code:
        "FORNITORE_SENZA_COPIA", …}]` e provava la funzione che la traduceva.
        Ma quel campo il servizio locale **non l'ha mai mandato**: una
        compilazione riuscita non ha errori per fornitore — o si compila tutto,
        o `run_writer` si ferma e la pagina passa da `renderCompileFailure`.
        Era codice vivo solo nel suo test, ed è stato tolto il 17 agosto 2026;
        il contratto vero lo inchioda
        `test_web_app.LaCompilazioneRiuscitaNonHaErroriPerFornitoreTests`.

        Al suo posto resta la cosa che va difesa davvero: senza avvisi il
        riquadro è quello pieno, non quello «a metà».
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
        """⚠ Difesa della 6d che nessun test distingueva.

        Il revisore dell'integrità dei test l'ha spenta e sono restati 1239 test
        verdi: la cartella c'è e i documenti si scaricano, ma se l'audit non si
        legge fornitori, totale e righe non si possono mostrare — e la pagina
        deve dirlo, non far vedere un totale che nessuno ha calcolato.
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
        # I documenti restano scaricabili: è l'audit a mancare, non i file.
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
        """Il documento c'è ed è giusto, ma il nome leggibile non si è potuto dare.

        ⚠ Prima questa notizia stava solo dentro `message`: il riquadro restava
        verde con il pulsante primario, come una consegna perfetta (revisione di
        regressione del 14 agosto 2026).
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
        # E il documento resta scaricabile: l'avviso non è un rifiuto.
        self.assertIn("/scarica/ordini.zip", html)

    def test_una_compilazione_pulita_resta_verde(self) -> None:
        preparazione = self.con_confronto(pipeline=None, passo=3) + """
          state.compileResult = { ok: true, message: "3 listini pronti.", outputs: [], writerIssues: [], historyIssues: [] };
        """
        html = self.esegui("return renderCompileResult();", preparazione=preparazione)
        self.assertNotIn("results--partial", html)
        self.assertIn("3 listini pronti.", html)

    def test_l_annulla_dell_esclusione_non_sopravvive_a_un_confronto_nuovo(self) -> None:
        """Rimetteva fornitore e conferma del confronto PRECEDENTE su un prodotto nuovo."""

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
    """Quello che non si esegue: si controlla nel sorgente, e si dichiara.

    ⚠ Queste NON sono prove eseguite.  Guardano il testo di `app.js` e di
    `styles.css`.  Coprono due cose che il banco non puo' vedere: i gestori di
    click (registrati su un `addEventListener` finto) e l'esistenza delle regole
    CSS delle classi nuove.  Un foglio di stile che dichiara la classe ma la
    disegna male passa lo stesso: qui non c'e' un browser.
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
        # E la vecchia riga, che li confondeva, non c'e' piu'.
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
            # Riorganizzazione delle tre pagine, 17 agosto 2026.
            "offer-grid__head", "offer-card__label", "offer-card__numero",
            "offer-card__intera", "offer-grid__mancanti", "alert--gruppo",
            "alert__elenco", "dropzone--riga", "button--small",
            "supplier-totals-strip__item--totale",
            # Le due voci nuove, dentro il disegno e non attaccate sopra.
            "confirmation__gia", "doc-columns__note--manca",
            # Il prezzo al pezzo in pagina 3, 21 settembre 2026.
            "order-lines__pezzo", "order-lines__pezzo-etichetta", "order-lines__intestazione",
        ):
            with self.subTest(classe=classe):
                self.assertIn(f".{classe}", self.css)

    def test_le_colonne_dei_numeri_non_sono_larghe_quanto_il_contenuto(self) -> None:
        """Le due colonne che ballavano nel riepilogo.

        ⚠ Prova a sottostringa: qui non c'è un browser, quindi si guarda la
        regola e non l'effetto. Che le colonne si corrispondano davvero è stato
        misurato nel browser (le righe da «9,06 €» e da «17,85 €» avevano
        l'ultima colonna larga 37 px e 44 px, e con lei si spostavano i comandi
        − 1 + × di tutta la riga).
        """

        regola = self.css.split(".order-lines li {", 1)[1].split("}", 1)[0]
        self.assertIn("grid-template-columns: minmax(0, 1fr) 5.5rem 8.75rem 5.5rem 5.5rem;", regola)
        self.assertNotIn("auto", regola.split("grid-template-columns:", 1)[1].split(";", 1)[0])
        # Anche nella vista per prodotto: l'intestazione si allinea solo se
        # nessuna colonna dopo il nome dipende dal contenuto.
        per_prodotto = self.css.split(".order-lines--product li {", 1)[1].split("}", 1)[0]
        self.assertIn("minmax(8rem, 0.45fr) 5.5rem 8.75rem 5.5rem 5.5rem;", per_prodotto)

    def test_sotto_i_1050_px_il_prezzo_al_pezzo_resta_accanto_al_totale(self) -> None:
        """Misurato nel browser il 21 settembre 2026, vista per prodotto a 900 px:
        con la quantità su una riga sua, la riga dei comandi metteva il prezzo al
        pezzo sotto il fornitore e il totale a metà riga. Sotto i 760 px vince la
        regola dei telefoni (una riga sua, sotto il totale), perché viene dopo
        con la stessa forza."""

        media_1050 = self.css.split("@media (max-width: 1050px)", 1)[1].split("@media", 1)[0]
        self.assertIn(".order-lines--product .order-lines__pezzo {\n    grid-column: 4;", media_1050)
        self.assertIn(".order-lines--product li > strong {\n    grid-column: 5;", media_1050)
        media_760 = self.css.split("@media (max-width: 760px)", 1)[1].split("@media", 1)[0]
        self.assertIn(".order-lines .order-lines__pezzo {\n    order: 1;\n    grid-column: 1 / -1;", media_760)


class UnaRichiestaFallitaNonSparisce(BancoDiProva):
    """Una richiesta andata male non deve avere lo stesso aspetto del «niente».

    Sono i tre punti in cui la pagina lo faceva: gli ordini in sospeso, il
    controllo dell'avanzamento del ricalcolo, e la risposta che dichiara JSON e
    non lo è.
    """

    def test_gli_ordini_in_sospeso_non_letti_si_dichiarano(self) -> None:
        """«Non c'è niente in sospeso» e «non l'ho potuto sapere» non sono uguali."""

        valore = self.esegui(
            "return loadPendingOrders().then(() => ({"
            " errore: state.history.errore,"
            " riquadro: renderPendingOrdersPanel(),"
            "}));",
            preparazione=self.con_confronto(pipeline=None, passo=2),
            # Nessuna risposta per /api/history/pending: la richiesta fallisce.
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
        """Con `{}` al posto dell'errore, /api/review dava «0 prodotti»."""

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
        """La conseguenza che conta: la pagina resta su un errore, non su zero prodotti."""

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

        # I primi controlli persi non sono una notizia; il quinto sì.
        self.assertEqual(valore["passi"], ["zitto", "zitto", "zitto", "zitto", "detto"])
        self.assertIn("Non so più a che punto è", valore["barra"])
        self.assertIn("Il confronto continua per conto suo", valore["barra"])

    def test_un_controllo_riuscito_cancella_l_avviso(self) -> None:
        """Il servizio che torna a rispondere si vede: l'avviso non resta appiccicato."""

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
    """Con dodici prodotti fermi si leggevano tre nomi e «e altri 9».

    Gli identificativi c'erano nella risposta del servizio — li manda apposta,
    uno per controllo non superato — e la pagina li buttava via: non c'era modo
    di ritrovare le righe da correggere fra cinquecento.
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
    """La scheda dichiara da quale versione dello stato è partita.

    Senza questo numero il servizio locale non ha modo di distinguere due schede
    aperte sullo stesso confronto — è la stessa run — e la seconda cancella il
    lavoro della prima appena scatta l'autosalvataggio.
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
        """Un servizio più vecchio non deve far mandare `undefined`."""

        valore = self.esegui(
            "return loadReview().then(() => snapshot().stateVersion);",
            risposte={"/api/review": REVISIONE},
        )

        self.assertEqual(valore, 0)

    def test_il_salvataggio_raccoglie_la_versione_nuova(self) -> None:
        """Chi ha appena salvato non deve vedersi rifiutare il salvataggio dopo."""

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
    """L'ordine dei prodotti nella pagina 2 — prove eseguite.

    Chiesto il 15 agosto 2026: «i prodotti nella comparazione devono essere
    ordinabili per ordine alfabetico».  Con cinquecento righe l'ordine del
    gestionale va benissimo per chi ha in mano il documento e non serve a
    niente a chi cerca un prodotto.
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
        """È l'ordine in cui chi ordina ha scritto i prodotti: non si perde."""

        self.assertEqual(self.nomi(None), [
            "ZUCCHERO DI CANNA", "NEVAL CREMA 100ML", "aceto di mele",
            "NEVAL CREMA 10ML", "Èlite salviette",
        ])

    def test_dalla_a_alla_z_conta_i_numeri_come_numeri(self) -> None:
        """«10ML» prima di «100ML», e minuscole e accenti al loro posto."""

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
        """`sort` ordina sul posto: senza copia riordinerebbe `state.review`.

        ⚠ Prima questa prova passava per `filteredProducts()`, e non provava
        niente: `filter` restituisce gia' un array nuovo, quindi togliere la
        copia dentro `sortedProducts` non si vedeva (controprova rimasta verde
        al primo giro). Si chiama la funzione con l'elenco vero, come farebbe
        chiunque la riusi.
        """

        prima = self.esegui(
            "state.filters.sort = 'nome'; sortedProducts(state.review.products);"
            "return state.review.products.map((prodotto) => prodotto.name);",
            preparazione=self.con_confronto(revisione=self.SCAFFALE, passo=2),
        )
        self.assertEqual(prima[0], "ZUCCHERO DI CANNA")

    def test_l_ordinamento_non_spegne_il_filtro_delle_offerte(self) -> None:
        """Cambiare l'ordine non è un filtro, e non deve cancellarne uno."""

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
    """I riquadri apribili non si richiudono da soli — prove eseguite.

    ⚠ «La barra Dettagli del ricalcolo si chiude dopo mezzo secondo quando la
    apro» (15 agosto 2026). Mezzo secondo e' la meta' del giro di polling:
    finche' la catena lavora la pagina si ridisegna ogni secondo, e `render()`
    non aggiorna il contenuto, lo **sostituisce** (`innerHTML = …`). L'apertura
    di un `<details>` vive nel nodo, non nel testo che l'ha prodotto.

    Quello che questa classe NON copre, e va detto: il banco non ha un DOM
    vero, quindi qui non si prova che il gesto sull'elemento arrivi al
    listener. Si prova la funzione che il listener chiama, e si prova che il
    disegno rispetti la memoria anche al secondo giro.
    """

    def test_la_barra_del_ricalcolo_riparte_aperta_se_era_aperta(self) -> None:
        html = self.esegui(
            'state.aperti["ricalcolo"] = true; return renderAvanzamentoPipeline();',
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertIn('class="pipeline-details" data-ricorda="ricalcolo" open', html)

    def test_se_non_l_hai_aperta_resta_chiusa(self) -> None:
        """L'`open` non è inchiodato nel markup: dipende davvero dalla memoria."""

        html = self.esegui(
            "return renderAvanzamentoPipeline();",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertIn('data-ricorda="ricalcolo"', html)
        self.assertNotIn("open", html.split("<summary>")[0].split("pipeline-details")[1])

    def test_due_ridisegni_di_fila_la_lasciano_aperta(self) -> None:
        """È la traduzione fedele del timer da un secondo, senza DOM."""

        due = self.esegui(
            'state.aperti["ricalcolo"] = true;'
            "return [renderAvanzamentoPipeline(), renderAvanzamentoPipeline()];",
            preparazione=self.con_confronto(pipeline={"stato": "IN_CORSO", "fasi": []}),
        )

        self.assertTrue(all('data-ricorda="ricalcolo" open' in html for html in due))

    def test_il_gesto_scrive_aperto_e_scrive_chiuso(self) -> None:
        """La funzione che il listener chiama, provata davvero.

        ⚠ Chiudendo, la chiave **resta** e vale `false`.  Fino al 22 agosto
        2026 veniva cancellata, e per i riquadri che nascono chiusi era lo
        stesso; da quando l'elenco di un fornitore nel riepilogo nasce APERTO
        non lo e' piu': cancellare la chiave gli faceva ritrovare il valore di
        partenza al ridisegno successivo, cioe' lo riapriva sotto le dita.
        «Mai toccato» e «chiuso da chi ordina» devono restare due cose diverse.
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
        """Il seguito del test qui sopra, dal lato del disegno.

        Non basta che la memoria dica `false`: deve essere `apribile()` a
        guardarla.  Un riquadro che nasce aperto e che si e' chiuso deve
        restare chiuso anche dopo dieci ridisegni.
        """

        aperture = self.esegui(
            "const finto = { dataset: { ricorda: 'prova' }, open: false };"
            "ricordaApertura(finto);"
            "return [apribile('prova', true), apribile('prova', true), apribile('mai-toccato', true)];",
            preparazione=self.con_confronto(pipeline=None),
        )

        self.assertNotIn("open", aperture[0])
        self.assertNotIn("open", aperture[1])
        # Uno che nessuno ha toccato nasce aperto: e' il valore di partenza.
        self.assertIn("open", aperture[2])

    def test_due_schede_diverse_non_si_aprono_insieme(self) -> None:
        """La chiave porta dentro il prodotto: sono riquadri diversi."""

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
    """Un prodotto c'e', ma la pagina risponde «Nessun prodotto trovato».

    ⚠ Il 15 agosto 2026: «in prova2 c'e' un prodotto che si chiama KALIDERMA,
    ma se lo cerco sul comparatore non lo trovo».  Era nell'elenco, aveva la
    sua quantita', ed era fra gli esclusi — e un escluso non compare in nessun
    filtro tranne «Esclusi», senza che una parola lo dica.
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

    # Gli esclusi vivono in un insieme a parte, riempito al caricamento del
    # confronto: qui si prepara come lo preparerebbe un `excluded: true`
    # arrivato dal servizio.
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
        """«Continua a dirmi 459 prodotti»: il numero era giusto e non lo diceva."""

        html = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(revisione=self.ELENCO, passo=2) + self.ESCLUSI,
        )

        self.assertIn("2 dall’elenco del gestionale", html)
        self.assertIn("1 espositore dei listini", html)
        self.assertIn("1 aggiunto a mano", html)


class IlNumeroCheDeveTornareColGestionale(BancoDiProva):
    """«Vedendo lo stesso numero del gestionale, so che il documento è quello giusto».

    Il pulsante «Continua» contava tutto l'elenco — gestionale più espositori dei
    listini più prodotti aggiunti a mano — e sulla scheda del documento c'era un
    numero diverso, senza niente che spiegasse la differenza. Due numeri che non
    tornano fanno sospettare che il programma abbia letto un altro file.

    Prove eseguite: si chiamano `comandoContinua` e `renderNumeriPipeline` vere.
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

        # Quattro prodotti in elenco, due dal gestionale: sul pulsante vanno i due.
        self.assertEqual(continua["etichetta"], "Continua con 2 prodotti del gestionale →")

    def test_il_pulsante_dice_che_quel_numero_e_del_gestionale(self) -> None:
        # Senza «del gestionale» il numero piu' basso sembrerebbe un elenco
        # dimezzato invece della parte che deve tornare con il documento.
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
        # Un secondo numero uguale al primo non aggiunge niente e va tolto: è la
        # regola sui messaggi che si scrivono solo se servono a chi legge.
        html = self.esegui(
            'return renderNumeriPipeline({ documenti: 5, fornitori: 4, prodotti: 451, prodottiGestionale: 451 });',
            preparazione=self.con_confronto(),
        )

        self.assertIn("451 prodotti", html)
        self.assertNotIn("dal gestionale", html)


class QualiColonneLegge(BancoDiProva):
    """«Voglio vedere l'assegnazione delle colonne»: prima non c'era da nessuna parte.

    Prove eseguite: si chiama `renderFileCard` vera con lo stato vero.
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
        # `render()` riscrive l'innerHTML ogni secondo durante un ricalcolo:
        # senza `apribile()` il riquadro si richiuderebbe sotto le dita.
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
    """«I prodotti cominciano dopo la riga che dice LISTINO», detto in pagina.

    Il servizio sa gia' risolvere la regola a ogni lettura (`data_start_marker`,
    commit `d073b19`); la pagina invece sapeva solo far digitare un numero, e un
    numero non sopravvive a una settimana: su QUERCIA le righe 7-67 sono un blocco
    promozionale — prezzi che sono valorizzazioni di omaggi — e il listino vero
    comincia alla 69, dopo l'unico `A68 = "LISTINO"`.  La settimana prossima
    quel blocco e' lungo un'altra cosa e il 69 taglierebbe nel posto sbagliato
    **senza dire niente**.

    Prove eseguite: si chiamano `renderSchemaDocument`, `schemaApplicaSeparatore`
    e `schemaMappingPayload` vere, con lo stato vero.
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
        """Lo stato dopo aver scelto il separatore, applicato dalla funzione vera."""

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
        # E' la riga che il 15 agosto si poteva solo indovinare aprendo Excel.
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
        # Il campo si omette: `{}` farebbe rifiutare la mappatura dal servizio,
        # e una regola inventata dal programma sarebbe peggio del numero.
        payload = self.esegui(
            "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )

        self.assertNotIn("dataStartMarker", payload["mappings"][0])

    def test_un_testo_accorciato_diventa_una_regola_che_contiene(self) -> None:
        # I separatori veri portano dentro la settimana («PROMO DAL 30/07 AL
        # 27/08»): il pezzo stabile va confrontato con «contiene».
        payload = self.esegui(
            self.scelto("LIST") + "return schemaMappingPayload();",
            preparazione=self.preparazione(),
        )

        self.assertEqual(payload["mappings"][0]["dataStartMarker"], {
            "column": 1, "match": "contains", "text": "LIST", "offset": 1,
        })

    def test_un_separatore_accorciato_dal_profilo_non_chiede_l_uguaglianza(self) -> None:
        # Misurato sul QUERCIA vero: cinque separatori su sei sono righe
        # promozionali lunghe, che il profilo taglia a 80 caratteri e chiude
        # con i puntini. «E' uguale a» su un testo mozzato viene rifiutato dal
        # servizio, che confronta con la cella intera.
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
        # Il difetto vero: si poteva digitare 69 e continuare a vedere le righe
        # del blocco promozionale, senza mai vedere la riga del taglio.
        html = self.esegui(
            self.scelto() + "return renderSchemaPreview(state.schemaMapping.data.documents[0]);",
            preparazione=self.preparazione(),
        )

        self.assertIn("is-break", html)
        self.assertIn("PRODOTTO VERO", html)

    def test_il_numero_non_si_scrive_a_mano_finche_c_e_la_regola(self) -> None:
        # Due sorgenti per la stessa riga e' il modo di mandare al servizio una
        # regola che descrive una riga diversa da quella dichiarata.
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
    """I difetti misurati sulla pagina viva il 15 agosto 2026.

    Tutti della stessa famiglia: la stessa cosa scritta due volte nella stessa
    schermata, oppure scritta quando non serve a nessuno.  Prove eseguite.
    """

    # Lo stesso avviso arriva da due parti: dentro il confronto (`warnings`) e
    # dentro lo stato della catena (`avvisi`).  E' il caso vero:
    # QUANTITA_RIPRESE_DAL_GESTIONALE le manda tutte e due.
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
        """Uno nel riquadro in cima, uno accanto all'esito della catena."""

        html = self.pagina(self.COMPLETATO, revisione=self.revisione(warnings=[self.AVVISO]))

        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_un_avviso_che_solo_la_catena_conosce_si_vede(self) -> None:
        """La difesa toglie i doppioni, non gli avvisi."""

        solo_della_catena = {**self.AVVISO, "code": "COMPILAZIONE_DA_RICONFIGURARE",
                             "title": "La compilazione va ricontrollata"}
        pipeline = {**self.COMPLETATO, "avvisi": [self.AVVISO, solo_della_catena]}
        html = self.pagina(pipeline, revisione=self.revisione(warnings=[self.AVVISO]))

        self.assertIn("La compilazione va ricontrollata", html)
        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_senza_riquadro_in_cima_l_avviso_della_catena_resta(self) -> None:
        """Se il confronto non lo porta, l'unico posto in cui si legge è la catena."""

        html = self.pagina(self.COMPLETATO, revisione=self.revisione(warnings=[]))

        self.assertEqual(html.count("Le quantità sono quelle dell&#039;elenco"), 1)

    def test_la_riga_tecnica_dello_schema_non_si_legge_quando_va_tutto_bene(self) -> None:
        """«Schema riconosciuto dal registro (larice_v1, confidenza 0.97)», per cinque volte."""

        documenti = self.revisione(files=[
            {"name": "ordine2.xlsx", "kind": "Gestionale", "supplier": "Gestionale", "status": "ready",
             "message": "Schema riconosciuto dal registro (gestionale_v1, confidenza 0.99): nessuna chiamata AI."},
            {"name": "listino.xlsx", "kind": "Listino", "supplier": "LARICE", "status": "ready",
             "message": "Schema riconosciuto dal registro (larice_v1, confidenza 0.97): nessuna chiamata AI."},
        ])
        html = self.pagina(self.COMPLETATO, revisione=documenti)

        self.assertNotIn("Schema riconosciuto dal registro", html)
        # I documenti restano, con il loro esito.
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
        """Erano tutti e due «Rifai il confronto»: sembravano due comandi."""

        html = self.pagina(self.COMPLETATO)

        self.assertIn("<h3>Confronto dei listini</h3>", html)
        self.assertEqual(html.count("Rifai il confronto"), 1)

    def test_il_messaggio_che_dice_che_e_andato_bene_non_si_scrive(self) -> None:
        html = self.pagina(self.COMPLETATO)

        self.assertNotIn("Confronto aggiornato: 457 prodotti", html)
        # I numeri della run restano, e sono quelli che si guardano.
        self.assertIn("5 documenti", html)
        self.assertIn("4 fornitori", html)
        # ⚠ «prodotti» resta di proposito: e' il numero che deve tornare con le
        # righe scritte sulla scheda del documento, ed e' una richiesta di
        # Daniele del 15 agosto (`comandoContinua` e `prodottiGestionale`).
        self.assertIn("457 prodotti", html)
        # «casi da valutare» e «decisi» stanno gia' dentro «Dettagli del
        # ricalcolo», con piu' contesto.
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
        """La frase stava sotto il pulsante, sempre. Serve qui, una volta."""

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
        """Erano quattro in fila. Le Impostazioni non sono un passo del lavoro."""

        html = self.pagina(self.COMPLETATO)
        coda = html.split('<div class="page-actions">', 1)[1]

        self.assertEqual(coda.count("<button"), 1)
        self.assertIn('data-action="next"', coda)
        # Il comando non è sparito: è salito in testa alla pagina.
        testa = html.split('<div class="page-heading">', 1)[1].split("</div>\n    </div>", 1)[0]
        self.assertIn('data-action="apri-impostazioni"', testa)

    def test_la_spiegazione_del_ricalcolo_si_legge_solo_la_prima_volta(self) -> None:
        prima_volta = self.pagina(None, revisione=self.revisione(products=[]))
        self.assertIn("ci vogliono alcuni minuti", prima_volta)

        dopo = self.pagina(self.COMPLETATO)
        self.assertNotIn("ci vogliono alcuni minuti", dopo)

    def test_a_ricalcolo_finito_non_resta_una_barra_piena(self) -> None:
        """Una barra piena è l'avviso «ha funzionato» disegnato invece che scritto."""

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
        """Da solo in fondo al riquadro sembrava il pulsante della pagina."""

        html = self.pagina(self.COMPLETATO)
        intestazione = html.split('<section class="panel import-panel pipeline-panel">', 1)[1]
        intestazione = intestazione.split('class="panel__header"', 1)[1].split("</div>\n      </div>", 1)[0]

        self.assertIn('data-action="avvia-pipeline"', intestazione)


class IlConfrontoDeiFornitoriEUnaTabella(BancoDiProva):
    """Pagina 2: le stesse quattro etichette dentro il riquadro di ogni fornitore.

    Su una pagina da venti prodotti erano trecentoventi etichette lette per
    leggere trecentoventi numeri, e i numeri non erano incolonnati.  Prove
    eseguite.
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
        # Due offerte disponibili, una sola intestazione per colonna.
        self.assertEqual(html.count("Prezzo per pezzo"), 3, "una in testa più una per riga, come etichetta nascosta")
        self.assertEqual(html.count("<span>Prezzo per pezzo</span>"), 1)

    def test_i_numeri_di_ogni_fornitore_stanno_in_una_riga(self) -> None:
        html = self.scheda()
        righe = html.count('<label class="offer-card')

        self.assertEqual(righe, 2)
        self.assertIn("89,50", html)
        self.assertIn("95,20", html)

    def test_le_etichette_restano_per_chi_legge_con_la_voce(self) -> None:
        """Nascoste dal foglio di stile, non tolte dal markup."""

        html = self.scheda()

        self.assertIn('class="offer-card__label">Prezzo per pezzo</span>', html)
        self.assertIn(".offer-card__label", STYLES.read_text(encoding="utf-8"))

    def test_i_fornitori_che_non_hanno_il_prodotto_stanno_in_una_riga_sola(self) -> None:
        html = self.scheda()

        self.assertEqual(html.count("non ce l’hanno nel listino di adesso"), 1)
        self.assertIn("NOCE e CIPRESSO: non ce l’hanno nel listino di adesso.", html)

    def test_un_prodotto_senza_nessuna_offerta_non_riempie_la_scheda(self) -> None:
        """Quattro riquadri che dicevano quattro volte la stessa cosa."""

        html = self.scheda(self.SENZA_NESSUNA_OFFERTA)

        self.assertEqual(html.count("non ce l’hanno nel listino di adesso"), 1)
        self.assertIn("BETULLA, LARICE, NOCE e CIPRESSO", html)
        # E senza intestazioni: non c'è nessuna colonna da intestare.
        self.assertNotIn("offer-grid__head", html)

    def test_un_fornitore_con_un_motivo_suo_lo_tiene(self) -> None:
        """Il raggruppamento non deve mangiare un avviso specifico."""

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
        """«…1 cartone di RESALINA … in omaggio: … Omaggio: RESALINA …»."""

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
        """«1 controlli», «1 offerte», «1 pezzi per collo»."""

        una_sola = {
            **self.ELENCO,
            "products": [{
                **self.ELENCO["products"][0],
                "offers": [self.ELENCO["products"][0]["offers"][0], {"supplierId": "larice", "available": False}],
                "selectedSupplierId": "betulla",
            }],
        }
        html = self.scheda(una_sola)

        # ⚠ Diceva «1 offerta». In un negozio «offerta» vuol dire sconto, e
        # quel numero conta i fornitori che il prodotto ce l'hanno: adesso lo
        # dice, e si contano i fornitori distinti, non le righe di listino.
        self.assertIn("<span>1 fornitore ce l’ha</span>", html)
        self.assertNotIn("1 fornitori", html)
        self.assertNotIn("offerta</span>", html)

    def test_il_totale_dell_ordine_sta_nella_fascia_che_resta_visibile(self) -> None:
        """Era un badge dentro l'intestazione dell'elenco, che scorre via."""

        pagina = self.esegui(
            "return renderQuantityStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=self.ELENCO, passo=2),
        )

        self.assertIn("supplier-totals-strip__item--totale", pagina)
        self.assertNotIn("Totale provvisorio", pagina)
        # Ed è lo stesso numero di prima: la somma dei fornitori. Un prodotto
        # solo, quindi il totale dell'ordine è il totale di quella riga.
        fascia = pagina.split("supplier-totals-strip__item--totale", 1)[1].split("</span>", 3)
        self.assertIn("190,40", "".join(fascia))


class GliAvvisiUgualiSiContano(BancoDiProva):
    """Pagina 3: cinquantatré riquadri prima di vedere i numeri dell'ordine.

    Otto bloccanti e quarantacinque avvisi, stampati uno per uno.  Ma i titoli
    erano quattro: «Conferma richiesta» otto volte, «Possibile prodotto
    CIPRESSO» trentanove.  A ripetersi era il titolo; il messaggio di ciascuno
    e' diverso e serve.  Prove eseguite.
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
        # Una volta sola in pagina: la seconda occorrenza è la chiave della
        # memoria di apertura, che nessuno legge.
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
        """Sotto la soglia raggruppare aggiunge un clic e non toglie niente."""

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
        """È il motivo per cui la compilazione non parte: non si nasconde."""

        html = self.html(self.avvisi(4, titolo="Conferma richiesta", bloccante=True))

        self.assertIn('<div class="alert alert--danger alert--gruppo" role="alert">', html)
        self.assertNotIn("<details", html)
        self.assertIn("Conferma richiesta · bloccante", html)

    def test_un_gruppo_di_avvisi_parte_chiuso_e_ricorda_l_apertura(self) -> None:
        """Sono decine e non fermano niente: chiusi. E `apribile()` li tiene aperti."""

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
        """Cinque listini con lo stesso problema sono un avviso, non cinque."""

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
    """Il verde in maiuscolo e il blu che gli rubava l'occhio. Prove eseguite."""

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
        """Il criterio di accettazione: 53 riquadri diventano 11.

        Qui in piccolo — otto avvisi con lo stesso titolo — perche' e' la
        stessa regola: a ripetersi era il titolo, non il messaggio.
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
        # E nessun messaggio perso.
        for indice in range(1, 9):
            self.assertIn(f"«PRODOTTO {indice}»", riepilogo)

    def test_un_solo_pulsante_primario(self) -> None:
        html = self.pagina()

        self.assertEqual(html.count("button--primary"), 1)
        self.assertIn('data-action="compile"', html.split("button--primary", 1)[1][:200])

    def test_dopo_la_compilazione_il_primario_diventa_lo_scarico(self) -> None:
        """⚠ A compilazione riuscita «Compila i listini» restava blu, largo e
        acceso SOPRA il riquadro dell'esito, mentre il passo successivo —
        scaricare lo zip da portare al fornitore — era il pulsante piu' piccolo
        e piu' in basso. Il piu' grande, in alto, ricompila e crea un'altra
        cartella datata."""

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
        """Sotto soglia diventava blu: con quattro fornitori sotto soglia
        sarebbero stati quattro primari contro il vero passo successivo."""

        html = self.pagina()
        comando = html.split('data-action="move-supplier"', 1)[0]

        self.assertIn("button--secondary", comando[-260:])
        # Il riquadro resta evidenziato: a dire che la soglia non c'è è il
        # colore, non una frase che ripete l'intestazione della scheda.
        self.assertIn("supplier-summary__move is-below", html)
        # Dentro la scheda del fornitore «al minimo d'ordine» si legge una
        # volta sola: nell'intestazione. (In cima alla pagina c'è l'avviso, che
        # è un altro posto e un'altra cosa.)
        scheda = html.split('<details class="supplier-summary"', 1)[1].split("</details>", 1)[0]
        self.assertEqual(scheda.count("al minimo d’ordine"), 1)

    def test_la_scheda_finale_non_ripete_le_promesse_gia_fatte(self) -> None:
        html = self.pagina()

        self.assertNotIn("Pronto per creare i listini?", html)
        self.assertNotIn("I documenti originali restano invariati", html)
        # La promessa che nessun'altra riga fa resta.
        self.assertIn("Nessun ordine viene inviato.", html)


class UnaConfermaGiaDataSiLeggeEeSiRevoca(BancoDiProva):
    """Le conferme, dentro la riga del prodotto a cui si riferiscono.

    Il magazzino (`app/conferme.py`) le tiene per **articolo** — codice a barre
    piu' nome — e non per riga, ed e' questo che le fa sopravvivere al listino
    della settimana dopo.  Ma finche' la pagina non lo diceva, una spunta gia'
    messa somigliava a un residuo da controllare: quello che mancava non era il
    motore, erano le due righe che spiegano da quando vale e come si toglie.
    Prove eseguite.
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
        """Senza risposta non c'è niente da aggiungere: si chiede, e si dice perché."""

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
        """Vale per l'articolo, non per la riga: è tutto il senso del magazzino."""

        html = self.blocco(self.GIA_DATA)

        self.assertIn("Vale per l’articolo", html)
        self.assertIn("non per la riga del listino", html)

    def test_e_dice_come_si_annulla(self) -> None:
        """La revoca funzionava da sempre e nessuna scritta la spiegava.

        ⚠ Dal 22 agosto 2026 si annulla con un pulsante, non togliendo una
        spunta: il sì e il no rispondono alla stessa domanda e si danno nello
        stesso modo. E a risposta data la domanda non si ripropone — resta
        quello che si è risposto e la via per cambiare idea, come fa il no nella
        griglia delle offerte.
        """

        html = self.blocco(self.GIA_DATA)

        self.assertIn("Cambio idea", html)
        self.assertIn("Riapre la domanda", html)
        self.assertNotIn("revoca", html.lower())
        # ⚠ E la casella non c'è più da nessuna parte: lasciarne una accanto al
        # pulsante vorrebbe dire due modi di dare la stessa risposta.
        self.assertNotIn("data-product-confirm", html)
        self.assertNotIn("type=\"checkbox\"", html)
        # A risposta data non si richiede: la domanda non è più aperta.
        self.assertNotIn("Sì, è lo stesso", html)
        self.assertNotIn("No, non è lo stesso", html)

    def test_tolta_la_spunta_la_domanda_torna_aperta(self) -> None:
        """Il ricordo del magazzino c'è ancora finché non si salva: scrivere
        «già confermato» sopra una casella vuota direbbe il contrario."""

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
        """CONFERME_NON_DISPONIBILI passa dal riquadro degli avvisi, non da un
        posto suo: è un avviso del confronto come gli altri."""

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
    """«Non è lo stesso articolo»: la frase sotto il pulsante, e i riquadri dopo.

    ⚠ Quella frase era un paragrafo di 293 caratteri uguale per tutti i
    prodotti, e finiva dicendo «Si torna indietro dal riquadro di LARICE qui
    sopra» — un riquadro che nel momento in cui la si legge non esiste, perché
    nasce DOPO il no.  Provato in pagina il 22 agosto 2026: su SUPERMICIONE la
    frase nominava BETULLA mentre il riquadro sopra era di LARICE.  La stessa
    regola era scritta tre volte — sotto il pulsante, nell'avviso che passa e
    nel riquadro del rifiuto — e nessuna delle tre diceva la cosa che serve
    prima di premere: dove va a finire QUESTO prodotto.  Prove eseguite.
    """

    def revisione(self, offerte: list[dict], *, rifiutate: list[str] = ()) -> dict:
        # ⚠ Una riga rifiutata NON e' disponibile: e' cosi' che il servizio la
        # rimanda in pagina (`available` falsa piu' `rifiutata`), ed e' la
        # coppia su cui la griglia la distingue da «non ce l'hanno».
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
        """La pagina sa già chi subentra: è la regola di `normalizeReview`, il
        più conveniente al pezzo. Chiederlo a chi ordina era chiederglielo due
        volte."""

        html = self.blocco([self.OFFERTA_LARICE, self.OFFERTA_BETULLA])

        self.assertIn("LARICE esce da questo prodotto e l’ordine passa a BETULLA", html)
        self.assertIn("0,29", html)

    def test_con_l_ultimo_fornitore_dice_che_il_prodotto_resta_da_reperire(self) -> None:
        html = self.blocco([self.OFFERTA_LARICE])

        self.assertIn("è l’ultimo fornitore rimasto", html)
        self.assertIn("Prodotti da reperire", html)
        self.assertIn("con la sua quantità", html)

    def test_non_promette_piu_un_riquadro_che_non_c_e_ancora(self) -> None:
        """Il ritorno indietro sta nel riquadro del rifiuto, che nasce dopo:
        annunciarlo prima mandava a guardare in su e non trovare niente."""

        html = self.blocco([self.OFFERTA_LARICE, self.OFFERTA_BETULLA])

        self.assertNotIn("qui sopra", html)
        self.assertNotIn("Si torna indietro", html)

    def test_la_frase_e_legata_al_pulsante_per_chi_non_la_vede(self) -> None:
        """Senza `aria-describedby` un lettore di schermo annuncia il pulsante e
        basta: la conseguenza resta un pezzo di testo qualunque, lì accanto."""

        html = self.blocco([self.OFFERTA_LARICE])

        self.assertIn('aria-describedby="rifiuto-nota-p1"', html)
        self.assertIn('id="rifiuto-nota-p1"', html)

    def test_mentre_la_risposta_va_il_pulsante_dice_che_sta_lavorando(self) -> None:
        """Il gemello che riapre la domanda diceva «Attendere…» dal primo
        giorno; questo si spegneva e basta, per tutto il tempo di un
        salvataggio piu' una risposta piu' il confronto riletto."""

        html = self.esegui(
            "state.matches.answering = 'p1:larice:rifiuto'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None, revisione=self.revisione([self.OFFERTA_LARICE]), passo=2),
        )

        self.assertIn("Attendere…", html)
        self.assertNotIn("No, non è lo stesso</button>", html)

    def test_una_risposta_su_un_altra_scheda_non_fa_attendere_questa(self) -> None:
        """Spento sì — una risposta per volta — ma non «Attendere…»: qui non si
        sta attendendo niente."""

        html = self.esegui(
            "state.matches.answering = 'p9:betulla:rifiuto'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(
                pipeline=None, revisione=self.revisione([self.OFFERTA_LARICE]), passo=2),
        )

        self.assertIn("disabled", html)
        self.assertIn("No, non è lo stesso</button>", html)
        # E vale per tutte e due le risposte: spente, ma nessuna sta attendendo.
        self.assertIn("Sì, è lo stesso</button>", html)
        self.assertNotIn("Attendere…", html)

    def test_quanto_dura_un_no_si_legge_una_volta_sola(self) -> None:
        """Due fornitori rifiutati davano due copie della stessa frase di una
        settimana, una dentro ciascun riquadro."""

        html = self.griglia([self.OFFERTA_LARICE, self.OFFERTA_BETULLA],
                            rifiutate=["larice", "betulla"])

        self.assertEqual(html.count("se la settimana prossima ne propone un'altra"), 1)
        self.assertEqual(html.count("Rifiutata da te"), 2)
        self.assertEqual(html.count("Cambio idea: è lo stesso"), 2)

    def test_senza_nessun_rifiuto_la_regola_non_si_scrive(self) -> None:
        html = self.griglia([self.OFFERTA_LARICE, self.OFFERTA_BETULLA], rifiutate=[])

        self.assertNotIn("Un no vale finché", html)

    def test_la_scheda_dice_quanti_fornitori_hai_escluso_tu(self) -> None:
        """«0 fornitori ce l'hanno» su un prodotto rifiutato a tutti è la stessa
        bugia che il riquadro delle offerte ha già smesso di dire."""

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
    """Le condizioni commerciali, dichiarate dalla pagina invece che a mano.

    ⚠ Il buco misurato su QUERCIA il 17 agosto 2026: un fornitore nuovo si legge
    e si compila dalla pagina, ma le sue offerte no —
    `commercial_conditions` si scriveva a mano dentro `references/adapters.json`
    e ce l'aveva solo LARICE.  Prove eseguite.
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
        # Dentro «Altre colonne», che parte chiuso: non aggiunge niente alla
        # pagina di chi non ha offerte da dichiarare.
        self.assertIn("Altre colonne", html)

    def test_finche_non_c_e_la_colonna_non_si_chiede_altro(self) -> None:
        """Una forma senza colonna non è una dichiarazione: è una casella a vuoto."""

        html = self.documento()

        self.assertNotIn('data-schema-field="commercialLayout"', html)
        self.assertNotIn("Nome dell’articolo in omaggio", html)

    def test_scelta_la_colonna_si_chiede_come_sono_scritte(self) -> None:
        html = self.documento(self.SCELTA)

        self.assertIn('data-schema-field="commercialLayout"', html)
        self.assertIn("Ogni riga porta la sua offerta", html)
        self.assertIn("Un blocco di righe per ogni offerta", html)
        # Le due colonne che solo «a blocchi» pretende restano fuori.
        self.assertNotIn("Nome dell’articolo in omaggio", html)

    def test_a_blocchi_si_chiedono_le_colonne_che_quella_forma_pretende(self) -> None:
        """Il servizio rifiuta la dichiarazione senza: meglio chiederle adesso."""

        html = self.documento(self.SCELTA + 'state.schemaMapping.values["prof-1"].commercialLayout = "blocchi";')

        self.assertIn("Nome dell’articolo in omaggio", html)
        self.assertIn('data-schema-column="reward_description"', html)
        self.assertIn('data-schema-column="discount"', html)

    def test_la_dichiarazione_arriva_al_servizio(self) -> None:
        """Senza `commercialConditions` nel corpo il servizio non guarderebbe
        nemmeno la colonna scelta, e la domanda sarebbe senza effetto."""

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
        """`null` non è un vuoto da nascondere: è una cosa da fare."""

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
        """Non è un fornitore: la domanda non ha risposta possibile."""

        html = self.esegui(
            'return renderFileCard({ id: "f1", name: "gestionale.xlsx", kind: "Gestionale", '
            'supplier: "Gestionale", status: "ready", rows: 451, message: "" });',
            preparazione=self.con_confronto()
            + 'state.colonneDocumenti = { caricate: true, caricando: false, runId: "run-1", motivo: "", errore: "", '
              'perNome: { "gestionale.xlsx": { fileName: "gestionale.xlsx", role: "master", sheet: "S", '
              'headerRow: 1, dataStartRow: 2, origin: "registro", columns: [], commercialConditions: null } } };',
        )

        self.assertNotIn("non le legge nessuno", html)


# --- I cinque difetti visti usando il programma, 17 agosto 2026 --------------


class LoScontoDelFornitoreSiRilegge(BancoDiProva):
    """«Inserisco lo sconto, la pagina si ricarica e torna a 0.» Prove eseguite.

    Il servizio lo salvava e i prezzi scendevano davvero: a mentire era il
    campo.  `normalizeReview` costruisce un oggetto nuovo campo per campo e
    `payload.state` — dove viaggiano gli sconti — non era fra quelli, quindi
    `state.review.state` non e' mai esistito e `renderStickySupplierTotals`
    leggeva sempre `{}`.
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
        """Sul disco lo sconto e' `{rate: 0.06, runId: …}`: e' il servizio a
        ridurlo a una percentuale, e questa e' la seconda difesa.  Senza,
        `escapeHtml` scriverebbe «[object Object]» dentro un campo numerico —
        cioe' lo stesso schermo vuoto di prima, con piu' strada per capirlo."""

        html = self.esegui(
            "return renderStickySupplierTotals();",
            preparazione=self.confronto_con_sconti(
                {"cipresso": {"rate": 0.06, "runId": "R1"}, "betulla": "8"},
            ),
        )

        self.assertNotIn("object Object", html)
        campo_cipresso = html.split('data-supplier-discount="cipresso"')[0].split("<label")[-1]
        self.assertIn('value=""', campo_cipresso)
        # Un numero scritto come testo resta un numero: e' il caso di un
        # confronto salvato da una versione precedente.
        self.assertIn('value="8"', html)


class LaMerceCheNessunoHaNonFermaLaCompilazione(BancoDiProva):
    """Un collo chiesto dal gestionale che nessun listino porta. Prove eseguite.

    Il servizio lo accetta dal 16 agosto 2026 — esce nell'elenco «Prodotti da
    reperire» — e la pagina lo dichiarava «Fornitore mancante · bloccante»
    chiedendo di mettere la quantita' a zero, cioe' di cancellare il numero che
    serve a reperirlo.  Sul confronto del 17 agosto erano 32 prodotti su 457.
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
        """La regola vecchia serve ancora: li' c'e' davvero una scelta da fare.

        Ci si arriva soltanto togliendo la scelta a mano: `normalizeReview`
        assegna sempre l'offerta piu' conveniente fra quelle disponibili, e le
        caselle dei fornitori nascono solo sulle offerte disponibili.  E' la
        guardia di quell'invariante, ed e' l'unico stato in cui la frase
        «scegli un'offerta disponibile» ha un'azione dietro.
        """

        voci = self.avvisi(
            self.prodotto(offers=[
                {"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True},
            ], warnings=[]),
            preparazione_extra='state.review.products[0].selectedSupplierId = "";',
        )

        self.assertIn("Fornitore mancante", [voce["title"] for voce in voci if voce["blocking"]])


class IlFiltroMostraSoloQuelloCheServe(BancoDiProva):
    """«Ha troppi elementi, non capisco.» Prove eseguite.

    Sette voci che si sovrapponevano: «Da verificare» prendeva qualunque avviso
    — quindi anche i prodotti che nessuno ha, gia' in «Nessuno ce l’ha» — e
    «Senza quantita'» era «Da ordinare» letto al contrario.
    """

    ELENCO = [
        {"id": "p1", "name": "Da ordinare", "quantity": 3, "selectedSupplierId": "cipresso",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}]},
        {"id": "p2", "name": "Da confermare", "quantity": 2, "selectedSupplierId": "cipresso",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True,
                     "requiresConfirmation": True}]},
        # ⚠ Porta il suo avviso, come sul confronto vero: e' quello che il
        # vecchio «Da verificare» raccoglieva, ed e' la ragione per cui i due
        # filtri mostravano lo stesso prodotto sotto due nomi diversi.
        {"id": "p3", "name": "Nessuno ce l’ha", "quantity": 1, "selectedSupplierId": "",
         "offers": [{"supplierId": "cipresso", "available": False}],
         "warnings": [{"id": "p3-senza-offerta", "code": "SENZA_OFFERTA_UTILIZZABILE",
                       "title": "Nessun fornitore ce l’ha", "productId": "p3",
                       "message": "Nessun listino caricato porta questo prodotto.",
                       "severity": "warning", "blocking": False}]},
        {"id": "p4", "name": "Senza quantita", "quantity": 0, "selectedSupplierId": "",
         "offers": [{"supplierId": "cipresso", "price": 10, "unitsPerOrderUnit": 6, "available": True}]},
        # ⚠ Non e' «nessuno ce l'ha»: i listini la riga ce l'avevano, e a
        # toglierla sei stato tu dicendo «non e' lo stesso articolo». Prima
        # finiva in «Nessuno ce l’ha» insieme a p3, sotto un nome che su di lui
        # diceva il falso.
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
        # Niente di escluso e nessun ordine in sospeso: due righe che si
        # leggerebbero per scoprire che sono vuote.
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
        """La voce nasce dai dati, non da un elenco scritto a mano nella barra."""

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
        """Un menu' che perde la voce selezionata mente su cosa si sta guardando."""

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
        # ⚠ «Nessuno ce l’ha» NON e' «Da confermare»: e' la sovrapposizione che
        # rendeva incomprensibile il filtro di prima, dove lo stesso prodotto
        # compariva in tutti e due sotto due nomi che promettevano due cose.
        self.assertEqual(nomi["to-confirm"], ["Da confermare"])
        # ⚠ E non e' nemmeno «Scartato da me». Su quel prodotto i listini la
        # riga ce l'avevano: a toglierla e' stato l'utente. I due elenchi sono
        # disgiunti perche' la mossa da fare e' diversa — uno si cerca altrove,
        # per l'altro puo' bastare rivedere una risposta.
        self.assertEqual(nomi["missing"], ["Nessuno ce l’ha"])
        self.assertEqual(nomi["rejected"], ["Scartato da me"])
        self.assertEqual(nomi["excluded"], [])

    def test_il_tipo_di_prodotto_si_chiede_solo_se_ce_n_e_piu_d_uno(self) -> None:
        """Sul confronto vero i «Kit» sono zero: una riga da leggere e scartare."""

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
    """«Sarebbe carino poter cliccare su "Vai al filtro" ed essere rimandati
    nella sezione di controllo.» Daniele, 20 agosto 2026. Prove eseguite.

    L'avviso diceva con quale filtro trovarli e poi lasciava andarci a mano:
    cambiare pagina, aprire l'elenco a tendina, ritrovare la voce. In pagina 3
    pesa di piu', perche' «Conferma richiesta · bloccante» dice che la
    compilazione non parte e non da' la strada per sbloccarla.
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
        """La controprova che il gestore esiste davvero: senza, resterebbe un
        pulsante che non fa niente e l'HTML sarebbe uguale."""

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
        # Ricerca e tipo restringerebbero l'elenco che l'avviso ha promesso.
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
        """Il punto 6: «Conferma richiesta · bloccante» ferma la compilazione,
        e adesso dice anche come sbloccarla."""

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
        """Li' il comando porterebbe dove si e' gia', e il pulsante per
        rispondere sta due centimetri sotto.

        ⚠ Il fornitore si toglie DOPO la normalizzazione: `normalizeReview`
        sceglie da sola la piu' conveniente quando nessuna e' scelta, quindi
        un prodotto che nasce senza fornitore non resta senza.
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
    """Punti 5 e 7 del 20 agosto 2026: «Da sapere prima di continuare» si
    ripeteva identico su pagina 1 e 2, e in pagina 3 tornava come «Avvisi».

    Ripetuti perdono peso: chi li vede tre volte smette di leggerli. Adesso i
    documenti stanno dove i documenti si cambiano, i prodotti dove si
    rispondono, e i titoli sono tre e diversi.
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
        """Ferma la compilazione: non lo si nasconde per ordine."""

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
        """La pagina 1 e' quella che si apre per prima: un avviso nuovo non
        sparisce mentre nessuno se ne accorge."""

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
    """«Sei serio? 81 avvisi?» Prove eseguite.

    Ottanta erano di prodotto, e stanno gia' sulla scheda di ciascuno in pagina
    2, dove c'e' il pulsante per rispondere; due righe di riepilogo li contano e
    dicono con quale filtro si trovano.
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

        # I due che riguardano l'ordine intero: il riepilogo dei prodotti senza
        # offerta e il minimo d'ordine non raggiunto. I 39 di prodotto no.
        self.assertIn("2 avvisi", html)
        self.assertNotIn("41 avvisi", html)

    def test_il_riepilogo_che_li_conta_tutti_resta(self) -> None:
        html = self.pagina()

        self.assertIn("Prodotti che nessun fornitore ha", html)

    def test_e_i_trentanove_non_si_stampano_uno_per_uno(self) -> None:
        html = self.pagina()

        self.assertNotIn("Possibile prodotto CIPRESSO", html)


class ComeIlFornitoreChiamaQuelCheVende(BancoDiProva):
    """L'EAN uguale non garantisce il colore. Prove eseguite.

    Parole di Daniele, 17 agosto 2026: «su oggetti colorati, anche con EAN
    esatto, il prodotto potrebbe essere di colore diverso, come già visto in
    passato». Il caso vero è `DOPLO PIATTI PIANI 20PZ`, dove il gestionale il
    colore non lo dice, BETULLA ha «…Polipropilene Bianco» con lo **stesso** EAN e
    CIPRESSO propone «PRIME PIATTI PIANI ROSSI». La riga mostrava soltanto i
    numeri: il nome del fornitore, unico posto in cui il colore è scritto, il
    servizio lo mandava e la pagina lo buttava.
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
        """Sta già in testa alla scheda: ripeterlo su ogni riga è rumore."""

        html = self.griglia([{
            "supplierId": "betulla", "price": 8.50, "unitsPerOrderUnit": 10, "available": True,
            "description": "DOPLO Piatto Bianco", "ean": "8059402029357",
        }])

        self.assertNotIn("EAN 8059402029357", html)

    def test_un_ean_diverso_si_dice(self) -> None:
        """Vuol dire che la riga e' stata agganciata per altra via: e' la prima
        cosa da guardare, non l'ultima."""

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
    """Sfogliare il listino di un fornitore e abbinare a mano. Prove eseguite.

    Il caso: `LUXA SAPONE LIQ. EROG.250ML` sta nel gestionale col codice
    4009428623194 e solo CIPRESSO lo usa; NOCE ha lo stesso articolo sotto
    8729721830575 e a 1,15 invece di 1,28. Dal testo non è deducibile — la
    variante `ORIGINAL` nel nome del gestionale non c'è — quindi la via è
    guardare il listino.
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
        """Un listino che ne mostra 8869 su 8881 senza dirlo nasconde.

        ⚠ «8881» e non «8.881»: in italiano i gruppi di migliaia si
        separano da cinque cifre in su (`minimumGroupingDigits` 2), ed è
        `Intl` a saperlo. Scriverlo qui col punto sarebbe una prova che
        passa solo dove la lingua è un'altra.
        """

        html = self.finestra()

        self.assertIn("8881 righe", html)
        self.assertIn("12 non ordinabili", html)

    def test_una_riga_non_ordinabile_si_vede_e_dice_perche_ma_non_si_abbina(self) -> None:
        html = self.finestra()

        self.assertIn("Non ordinabile: senza prezzo", html)
        self.assertNotIn('data-riga="5000"', html)

    def test_la_riga_su_cui_si_e_aperto_e_evidenziata(self) -> None:
        """Su ottomila righe, cercare a mano una cosa che il programma sa già
        è il modo di non usare lo strumento."""

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
        """⚠ Spostate in Impostazioni il 18 agosto 2026. Qui erano dentro un
        prodotto — per rileggerle bisognava aprirne uno qualunque — e dicevano
        due numeri di tredici cifre senza i nomi, cioè senza l'unica cosa con
        cui si giudica se la dichiarazione è giusta."""

        html = self.finestra(uguaglianze=[{
            "codici": ["4009428623194", "8729721830575"],
            "motivo": "scelta a mano dal listino NOCE, riga 4794",
        }])

        self.assertNotIn("Non sono lo stesso", html)
        self.assertNotIn("4009428623194 = 8729721830575", html)

    def test_da_ogni_prodotto_si_puo_aprire_il_listino(self) -> None:
        """E si apre sul fornitore che ha già una riga per questo prodotto: è
        anche il modo di controllare che sia davvero la stessa merce, che con
        l'EAN uguale non è garantito."""

        revisione = {**REVISIONE, "products": [self.PRODOTTO]}
        html = self.esegui(
            "return renderOfferGrid(state.review.products[0]);",
            preparazione=self.con_confronto(revisione=revisione, passo=2),
        )

        self.assertIn('data-action="apri-listino"', html)
        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga="900"', html)


class SfogliaApreIlFornitoreScelto(BancoDiProva):
    """«Sfoglia i listini» apre il listino del fornitore scelto. Prove eseguite.

    Prima apriva quello del **primo** fornitore dell'elenco che avesse una riga
    abbinata, e l'elenco e' nell'ordine fisso di `build_review_data.supplier_ids()`
    — betulla, larice, noce, cipresso — quindi coincideva con il fornitore
    scelto solo per caso. Sullo stato dell'ultima settimana (459 prodotti) il
    fornitore scelto e' BETULLA nel 27,5% dei casi: per tutti gli altri il
    pulsante apriva il listino di qualcun altro, con il fuoco su una riga che
    riguardava un altro fornitore.

    I quattro casi qui sotto sono quelli misurati: due sbagliavano e
    due indovinavano per caso.
    """

    ORDINE = ("betulla", "larice", "noce", "cipresso")

    def prodotto(self, scelto: str, righe: dict[str, int | None]) -> dict:
        """Un prodotto con un'offerta per fornitore, nell'ordine dell'elenco vero."""

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
        """Il caso che sbagliava: scelto NOCE, ma BETULLA viene prima e ha una riga."""

        html = self.sfoglia(self.prodotto("noce", {"betulla": 1751, "noce": 4755}))

        self.assertIn('data-supplier-id="noce"', html)
        self.assertIn('data-riga="4755"', html)

    def test_il_fornitore_scelto_vince_anche_con_tre_righe_prima(self) -> None:
        """L'altro caso che sbagliava: scelto CIPRESSO, che nell'elenco è ultimo."""

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
        """Scelto LARICE; BETULLA viene prima ma non ha nessuna riga agganciata."""

        html = self.sfoglia(self.prodotto("larice", {"betulla": None, "larice": 6277}))

        self.assertIn('data-supplier-id="larice"', html)
        self.assertIn('data-riga="6277"', html)

    def test_se_nessuno_ha_una_riga_si_apre_lo_stesso_quello_scelto(self) -> None:
        """Il caso «cercala a mano»: nessun fuoco da accendere, ma il listino
        giusto da aprire e' quello del fornitore scelto, non il primo."""

        html = self.sfoglia(self.prodotto("cipresso", {"betulla": None, "cipresso": None}))

        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga=""', html)


class IlComandoDeiListiniNonPromettteUnFornitoreSolo(BancoDiProva):
    """Il pulsante si chiama «Sfoglia i listini», al plurale e senza nomi.

    ⚠ Per un giorno si e' chiamato «Sfoglia il listino LARICE», col nome del
    fornitore che avrebbe aperto per primo.  Daniele l'ha tolto il 22 agosto
    2026: la finestra che si apre ha la tendina di tutti i fornitori e da li' si
    passa da uno all'altro senza chiuderla, quindi quel nome prometteva meno di
    quello che il comando fa — e lo prometteva una volta per scheda.  Quale
    listino si apre per primo resta la scelta di sempre, il fornitore scelto:
    quella e' provata qui sopra e non cambia.  Prove eseguite.
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
        """L'etichetta è generica, la scelta no: sono due cose diverse."""

        html = self.sfoglia("cipresso")

        self.assertIn('data-supplier-id="cipresso"', html)
        self.assertIn('data-riga="900"', html)


class UnaPropostaCheNonSiPuoAccettareLoDice(BancoDiProva):
    """Il «Sì» che rispondeva sempre «Risposta non registrata». Prove eseguite.

    La riga proposta nasceva dalla shortlist, che portava quattro colonne: senza
    i pezzi per collo il servizio la rifiuta.  Sul confronto del 17 agosto:
    **48 proposte, zero accettabili**.
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
        """`Math.max(1, …)` stampava «1 pezzo per collo» su un dato che non c'è."""

        html = self.scheda({**self.COMPLETO, "quantityFactor": None, "orderUnitPriceNet": None, "available": False})

        self.assertNotIn("1 pezzo per collo", html)


class LaRigaPropostaPortaIlMotivoDelloScarto(BancoDiProva):
    """4 settembre 2026, PC del negozio, LESTOX LEGNO PULITO 5IN1 750ML.

    Due difetti nella stessa scheda, e la stessa causa: la pagina non sapeva
    niente della riga proposta se non il suo prezzo.

    Il primo. La domanda «e' lo stesso?» arrivava con codice, EAN e prezzo, e
    senza la sola cosa che serve per rispondere — il motivo per cui l'analisi
    automatica aveva scartato quella riga, «e' una confezione da 2 pezzi,
    mentre l'articolo cercato e' singolo». Il motivo c'era nei dati e
    `normalizeCandidate` non lo copiava; al suo posto, dietro «Perche'
    compare?», il punteggio di somiglianza, che a chi ordina non dice niente.

    Il secondo. Sotto la tabella: «LARICE: non ce l'hanno nel listino di
    adesso», due centimetri sotto la riga di LARICE con EAN, codice e prezzo.
    Due frasi opposte sullo stesso fornitore nella stessa scheda.
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
        """La scheda intera: la domanda sta sopra la tabella, la frase sotto."""

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
        # Il servizio toglie l'avviso appena la risposta arriva
        # (`apply_match_overrides`): a decisione presa resta il riquadro della
        # risposta data, e la frase di sotto non deve tornare nemmeno li'.
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
        """Due riquadri chiedevano la stessa cosa con due nomi diversi."""

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
        """Il riquadro della risposta data dice «non e' lo stesso articolo»:
        sotto non ci puo' stare «non ce l'hanno»."""

        html = self.scheda(decisione="rejected")

        self.assertIn("NON \u00e8 lo stesso articolo", html)
        self.assertNotIn("non ce l\u2019hanno nel listino di adesso", html)

    def test_ma_un_fornitore_senza_riga_proposta_resta_nella_frase(self) -> None:
        """La frase serve e resta: si toglie solo chi si contraddiceva."""

        html = self.scheda(terzo_fornitore=True)

        self.assertIn("NOCE: non ce l\u2019hanno nel listino di adesso.", html)
        self.assertNotIn("LARICE e NOCE", html)


class LeUguaglianzeStannoInImpostazioni(BancoDiProva):
    """L'elenco delle dichiarazioni «questi due codici sono lo stesso articolo».

    Daniele, il 18 agosto 2026: «sarebbe carino averlo in impostazioni e poter
    vedere anche il nome del prodotto da gestionale e quello da listino,
    altrimenti non posso valutare la correttezza». Prima stava in fondo alla
    finestra «Sfoglia i listini» e mostrava due numeri di tredici cifre.
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
        """I nomi dicono se è lo stesso articolo, i codici dicono quale
        dichiarazione si sta togliendo: servono tutti e due."""

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
        # Il comando si chiama «Sfoglia i listini»: la finestra che apre ha la
        # tendina di tutti i fornitori, quindi il nome di uno solo sul pulsante
        # prometteva meno di quello che il comando fa.
        self.assertIn("Sfoglia i listini", html)

    def test_una_ricerca_senza_esiti_non_dice_che_non_ce_ne_sono(self) -> None:
        """«Nessuna dichiarazione» dopo aver scritto una parola direbbe una cosa
        falsa: quelle che ci sono restano, è la ricerca a non trovarle."""

        html = self.pannello(voci=[], query="marsiglia", totale=7)

        self.assertIn("Nessuna dichiarazione trovata", html)
        self.assertIn("7 dichiarate in tutto", html)


class LeConfermeSiScaricanoDaImpostazioni(BancoDiProva):
    """`conferme.db` e' la memoria «per sempre» del programma e non si legge.

    La conferma singola si vede sulla riga a cui si applica, con la data e
    l'articolo su cui vale; la vista d'insieme non c'era, e nemmeno una copia
    che si possa portare via da quel computer.
    """

    def test_c_e_un_collegamento_che_scarica_le_conferme(self) -> None:
        html = self.esegui("return renderSettingsConfermePanel();")

        self.assertIn("/api/conferme/esporta", html)
        # ⚠ Un `<a download>`, non un pulsante che chiama il servizio: quello
        # che torna e' un file da salvare, e la pagina non ha niente da farci.
        self.assertIn("download", html)
        self.assertIn("Scarica tutto quello che hai confermato", html)

    def test_dice_che_cosa_si_perde_se_si_perde_quel_file(self) -> None:
        """Un collegamento che dice solo «scarica» non lo preme nessuno: quel

        file e' l'unico modo di rileggere le conferme tutte insieme, ed e' la
        sola copia che si possa portare via da quel computer."""

        html = self.esegui("return renderSettingsConfermePanel();")

        self.assertIn("per sempre", html)
        self.assertIn("il file qui sotto è la tua copia", html)

    def test_il_pannello_e_dentro_la_pagina_delle_impostazioni(self) -> None:
        """Una funzione che nessuno chiama non è un comando: è codice morto."""

        html = self.esegui(
            "return renderSettingsPage();",
            preparazione="state.impostazioni.aperta = true; state.impostazioni.caricate = true;",
        )

        self.assertIn("/api/conferme/esporta", html)
        self.assertIn("Le conferme che hai dato", html)


class LaColonnaDellOrdineSiCambia(BancoDiProva):
    """La colonna in cui si scrivono le quantita' si sposta dalla scheda del
    documento, in pagina 1.

    Prima si poteva scegliere una volta sola, nella mappatura guidata, che si
    apre soltanto quando il programma non riconosce le colonne di un documento:
    per i fornitori conosciuti la pagina la mostrava e basta.
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
        """Senza una colonna dichiarata non c'e' una colonna da spostare: c'e'
        una configurazione di scrittura da creare, e quella nasce dalla
        mappatura guidata insieme a tutto il resto."""

        html = self.esegui(
            'return renderColonnaDellOrdine({supplierId: "quercia"}, {orderColumn: null});'
        )

        self.assertEqual(html, "")

    def test_ogni_colonna_dice_che_cosa_contiene(self) -> None:
        """Il programma rifiuta solo quello che sa dimostrare; il resto lo deve
        poter valutare chi il listino ce l'ha davanti."""

        html = self.finestra(10)

        # ⚠ «6380» e non «6.380»: in italiano i gruppi di migliaia si separano
        # da cinque cifre in su, ed è `Intl` a saperlo (vedi la nota in
        # `test_le_righe_scartate_si_contano_sempre`).
        self.assertIn("la leggo come Codice a barre", html)
        self.assertIn("6380 formule", html)
        self.assertIn("vuota", html)
        self.assertIn("6377 valori", html)

    def test_una_colonna_gia_occupata_non_si_puo_scegliere(self) -> None:
        html = self.finestra(10)

        prima = html.split('value="1"')[1].split(">")[0]
        self.assertIn("disabled", prima)

    def test_la_colonna_gia_in_uso_non_si_riconferma(self) -> None:
        """La finestra si apre su dove si e': un primario acceso su una scelta
        che non cambia niente e' un invito a premere per sbaglio."""

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
        """La frase e' quella della verifica vera — la stessa che comparirebbe
        al prossimo avvio — e deve arrivare intera a chi ha premuto."""

        html = self.finestra(10, errore="BETULLA non attivato: la cella J dell'intestazione non contiene ORDINE")

        # L'apostrofo esce da `escapeHtml`, quindi la frase si ritrova a pezzi:
        # quello che conta è che ci sia tutta, compreso il nome della cella.
        self.assertIn("BETULLA non attivato: la cella J dell", html)
        self.assertIn("intestazione non contiene ORDINE", html)
        self.assertIn("La colonna non è stata cambiata", html)


class IPulsantiSiPremonoDavvero(BancoDiProva):
    """Premere un pulsante fa la cosa giusta — non «il pulsante c'e'».

    ⚠ Fino al 19 agosto 2026 questa domanda non se la faceva nessuna prova.  I
    due gestori unici della pagina — quello di `#stepper` e quello di `#app`
    con i suoi ~52 rami `if (action === ...)` — erano registrati su un
    `addEventListener` che li buttava via, e la misura e' stata fatta:
    mettendo `if (true) return;` in testa a tutti e due restavano verdi 295
    collaudi, e fra i `data-action` che nessun file di `tests/` nominava
    c'erano `apply-supplier-move`, `undo-supplier-move`,
    `confirm-delete-upload` e `change-quantity`.

    Le prove qui sotto partono dalla regola che non si tocca: durante la
    compilazione non si cambia il fornitore sotto ai documenti che si stanno
    scrivendo, e quel divieto vive dentro una riga sola del gestore.
    """

    def test_fuori_dalla_compilazione_premere_avanti_cambia_pagina(self) -> None:
        """La controprova della prova dopo: senza questa, un gestore morto passerebbe."""

        passo = self.esegui(
            self.con_confronto(passo=1) + """
              premi("next");
              return state.currentStep;
            """,
            risposte={"/api/history/pending": {"pending": []}},
        )

        self.assertEqual(passo, 2)

    def test_durante_la_compilazione_premere_avanti_non_cambia_pagina(self) -> None:
        """La riga che lo vieta sta dentro il gestore, e prima non la eseguiva nessuno."""

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
        # E l'annulla si consuma: premerlo due volte non deve rifare niente.
        self.assertIsNone(esito["annullaRimasto"])

    def test_la_conferma_di_eliminazione_chiama_la_rotta_col_nome_del_listino(self) -> None:
        """Il nome viaggia nel corpo: sbagliarlo vuol dire cancellare un altro listino."""

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
        """Togliere un listino mentre la catena gira cambierebbe il risultato in corso."""

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
        """La prova che questo banco non e' inerte: la mutazione lo fa cadere.

        E' la stessa mutazione con cui il rilievo ha misurato il buco — `return`
        in testa al gestore di `#app` — fatta su una copia di `app.js`, e qui
        deve produrre un risultato diverso da quello della prima prova di questa
        classe.  Se un giorno restasse uguale, vorrebbe dire che `premi()` ha
        smesso di premere.
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
    """Rilievo [49] — le quattro prove che difendono `applySupplierMove()` in
    `tests/test_web_app.py:2094` (`SupplierMoveInterfaceTests`) guardano il
    TESTO sorgente della funzione, non quello che fa: una riga presente non e'
    una riga eseguita, e un `return;` messo in testa alla funzione le lascia
    tutte verdi. Quelle asserzioni non si toccano — sono l'unica difesa
    contro il ritorno di un cablato e lo dichiarano.

    Qui accanto, con lo stesso banco che preme del rilievo [46]: si costruisce
    uno `state.supplierMove` con un preventivo di spostamento gia' scelto
    dall'utente (la forma che arriva da `/api/suppliers/move-preview`), si
    preme `apply-supplier-move`, e si guardano i VALORI dopo — non il
    sorgente. Una funzione svuotata da un `return;` fa cadere queste prove.
    """

    def preventivo(self, **sovrascrizioni) -> dict:
        """Un preventivo che sposta il prodotto p1 da CIPRESSO a BETULLA, con
        l'opzione gia' scelta (`choiceId`): la stessa forma che
        `normalizeMovePreview` produce dalla risposta del servizio locale."""

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
        """La terza regola che non si tocca: «una conferma data su un'offerta
        non vale per un'altra». Il prodotto arriva confermato per CIPRESSO."""

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
        """La controprova che il rilievo chiede: `return;` come prima
        istruzione di `applySupplierMove()`, su una copia di `app.js`, deve
        rompere quello che le prove sopra misurano sui VALORI — non solo le
        quattro asserzioni sul sorgente in `test_web_app.py`."""

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
    """La stessa traduzione di `escapeHtml()` in `app.js:1184`, qui in Python:
    serve a controllare che una stringa velenosa non sia sparita — sarebbe
    verde per il motivo sbagliato — ma sia arrivata scappata."""

    return (
        valore.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#039;")
    )


class LEscapeSiVerificaEseguendoLePagine(BancoDiProva):
    """Rilievo [23] — il guardiano statico (`tests/test_consegna_interfaccia.py:312`,
    `test_ogni_testo_del_servizio_passa_da_escapeHtml`) guarda 3 funzioni di
    disegno su 85, e per costruzione non puo' guardarne di piu': legge il
    sorgente e cerca `escapeHtml(...)` intorno alle interpolazioni, quindi un
    aiutante che scappa al suo interno (`rigaDelFornitore`, `cella`,
    `promotionText`, `renderBadge`, `renderAlert`) lo confonderebbe e darebbe
    un falso allarme se lo si allargasse alle altre funzioni.

    Qui non si legge niente: si costruisce un `review` in cui OGNI campo di
    testo e' una stringa velenosa diversa — cosi' un fallimento dice anche
    QUALE campo perde l'escape — si disegnano le tre radici della pagina
    (`renderUploadStep`, `renderQuantityStep`, `renderCompileStep`: «le tre
    pagine hanno tre radici diverse», dice il commento dentro `render()` in
    `app.js`), e si pretende che nessuna stringa velenosa esca grezza
    nell'HTML prodotto. Non dipende da quale funzione disegna che cosa: se
    domani ne nasce una nuova che dimentica `escapeHtml`, questa prova la
    vede lo stesso.
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
                # Zero occorrenze grezze e' vero anche di un campo perso per
                # strada: si controlla che la versione scappata ci sia
                # davvero, non solo che quella grezza non ci sia.
                self.assertIn(
                    _scappata_come_in_app_js(marcatore), tutto,
                    f"«{campo}» non compare nemmeno scappato: dov'è finito?",
                )

    def test_un_escapeHtml_tolto_da_una_funzione_di_disegno_fa_cadere_la_prova(self) -> None:
        """La controprova che il rilievo chiede: si toglie un `escapeHtml(...)`
        da `rigaDelFornitore` — una delle quattro funzioni che il guardiano
        statico non copre — su una copia di `app.js`, e la prova sopra deve
        accorgersene."""

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
    """Rilievo [14] dell'onda 5 — l'unico di quest'area la cui conseguenza e'
    merce non ordinata, non un fastidio d'interfaccia.

    Il «×» del riepilogo azzera la quantita' e mette `confirmed = false`, e
    siccome `orderedProducts()` tiene solo i prodotti con quantita' maggiore di
    zero il prodotto sparisce dalla pagina 3 nello stesso istante: senza toast,
    senza conferma, e — fino a oggi — senza annullo. Per rimediare bisognava
    accorgersene, tornare alla pagina 2, ritrovare il prodotto e ridigitare una
    quantita' che nel frattempo non si ricorda. Se non ci si accorgeva, quel
    prodotto semplicemente non veniva ordinato.

    Prove eseguite: `premi()` esegue davvero i gestori di `app.js`.
    """

    def test_premere_il_x_azzera_la_quantita_e_toglie_la_conferma(self) -> None:
        """La controprova: senza questa, un annullo che non annulla passerebbe."""

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
        """⚠ Anche la conferma. Rimettere la sola quantita' farebbe tornare il
        prodotto nell'ordine con la compilazione bloccata e nessuna riga che
        dica perche'."""

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
        # E l'annullo si consuma: premerlo due volte non deve rifare niente.
        self.assertIsNone(esito["annullaRimasto"])

    def test_la_barra_di_annullo_compare_in_cima_alla_pagina_3(self) -> None:
        """In cima, e non dentro l'elenco: la riga da cui il prodotto e' appena
        sparito non esiste piu', e una barra infilata li' farebbe scorrere
        quello che si sta guardando. E non deve spingere giu' il pulsante
        «Compila», che sta in fondo alla pagina."""

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
        """La quantita' salvata era quella del confronto precedente: rimetterla
        su un confronto nuovo, dove il prodotto puo' avere offerte e prezzi
        diversi, e' la stessa trappola gia' chiusa per l'esclusione."""

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
        """⚠ Fra il «×» e l'annullo si puo' passare dalla pagina 2 e premere
        «Escludi dall'ordine» sullo stesso prodotto. Rimettendo la sola quantita'
        nasceva un prodotto insieme «Escluso» e con la sua quantita': contato
        nei subtotali e nel totale della pagina 3, e azzerato dal servizio
        locale, che per gli esclusi mette la quantita' a zero. La pagina
        mostrava un totale che nessun listino avrebbe portato."""

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
        """Il bersaglio piu' distruttivo non confina con quello che si preme di
        piu'. Nel markup i tre comandi della quantita' stanno dentro il loro
        gruppo, e il «×» ne resta fuori."""

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
    """Rilievo [5] dell'onda 5 — scegliere un fornitore o dare una conferma
    buttava via il fuoco della tastiera.

    I tre controlli del percorso decisionale — il gruppo di radio «quale
    fornitore», la spunta «confermo che e' lo stesso articolo» e la spunta
    «confermo gli ordini sotto il minimo» — chiamavano `render()`, che
    sostituisce l'intero `#app`: il nodo che aveva il fuoco veniva distrutto e
    il fuoco tornava su `<body>`. Per chi si muove con la tastiera questo rompe
    la navigazione a frecce dentro un gruppo di radio e obbliga a ritabulare
    dall'inizio della pagina dopo ogni conferma.

    Il rimedio esisteva gia' e si chiama `rerenderPreservingFocus()`: gli
    serviva soltanto una `data-focus-key` sui tre controlli.

    Prove eseguite: `cambia()` esegue davvero il gestore "change" di `app.js`,
    e il fuoco rimesso si conta perche' il nodo di ritorno lo registra.
    """

    def con_fuoco(self, chiave: str) -> str:
        """Finge che il fuoco sia su un nodo con quella chiave, e conta se
        qualcuno lo rimette dopo il ridisegno."""

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
        """⚠ Dal 22 agosto 2026 la conferma è un pulsante, non una casella, e
        il vincolo non cambia: premere non butta via il fuoco. Il pulsante non
        sparisce — diventa «Cambio idea» e tiene la stessa chiave — quindi il
        fuoco si rimette esattamente dov'era."""

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
        """Senza la chiave nel markup `rerenderPreservingFocus()` non trova
        niente ed esce senza fare niente: le tre prove qui sopra passerebbero
        contro un nodo che il browser non disegnerebbe mai."""

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
        """Cambiando fornitore la conferma puo' non essere piu' richiesta e
        sparire: `rerenderPreservingFocus()` non trova il nodo, esce senza fare
        niente, e il fuoco resta perso come prima — non peggio."""

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

        # Il fuoco non si rimette da nessuna parte…
        self.assertEqual(esito["fuochi"], [])
        # …ma la pagina si e' ridisegnata lo stesso: si ricade nel
        # comportamento di prima, non in uno peggiore.
        self.assertEqual(esito["ridisegni"], 1)


class IlFuocoSopravviveAiPulsantiCheRidisegnano(BancoDiProva):
    """Rilievo [15] dell'onda 5 — il fuoco moriva a ogni ridisegno.

    `rerenderPreservingFocus()` era applicato con criterio dove si DIGITA — il
    campo numerico della pagina 2 ce l'ha da sempre — e mai dove si PREME: delle
    tredici `data-focus-key` del file nessuna stava su un `<button>`, quindi
    premere «+» o «−» buttava via il fuoco e il Tab successivo ripartiva
    dall'inizio del documento. Lo stesso succedeva una volta al secondo per
    tutta la durata del ricalcolo, perche' il controllo dell'avanzamento
    ridisegnava con `render()`.

    Prove eseguite: `premi()` esegue i gestori veri.
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
        """Durante il ricalcolo il ridisegno e' ogni mille millisecondi: con
        `render()` il fuoco tornava su `<body>` sessanta volte al minuto."""

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
    """Rilievo [20] dell'onda 5 — Esc chiudeva due finestre su quattro.

    Le finestre sono quattro e tutte e quattro dichiarano `aria-modal="true"`:
    colonna d'ordine, visualizzatore listini, catalogo, spostamento fra
    fornitori. Il gestore di `keydown` ne conosceva due — catalogo e
    spostamento — e le altre due si chiudevano soltanto col «×». La piu'
    penalizzata era il visualizzatore listini, che e' la via d'uscita quando
    l'abbinamento automatico non ce l'ha fatta: si apre quando qualcosa e' gia'
    andato storto.

    Prove eseguite: `tasto("Escape")` esegue davvero il gestore di `window`,
    che fino a oggi nessuna prova eseguiva.
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
        """Prima si chiudeva solo col «×»."""

        aperto = self.esegui(
            self.con_confronto(passo=2) + """
              state.listino.aperto = true;
              tasto("Escape");
              return state.listino.aperto;
            """,
        )
        self.assertFalse(aperto)

    def test_esc_chiude_la_finestra_della_colonna_d_ordine(self) -> None:
        """E' la piu' consequenziale delle quattro: decide in quale colonna del
        listino del fornitore vengono scritte le quantita'. Prima si chiudeva
        solo col «×»."""

        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.colonnaOrdine.aperta = true;
              tasto("Escape");
              return state.colonnaOrdine.aperta;
            """,
        )
        self.assertFalse(aperta)

    def test_esc_chiude_quella_in_cima_e_lascia_stare_le_altre(self) -> None:
        """L'ordine e' quello del markup: `render()` aggiunge la colonna
        d'ordine dopo il contenuto di qualunque pagina, quindi e' sempre
        l'ultima disegnata e la prima a chiudersi."""

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
        """Solo il catalogo lo faceva. ⚠ `autofocus` non basta: non viene
        onorato sui nodi inseriti con `innerHTML`, ed e' esattamente il motivo
        per cui il catalogo lo rimandava gia' a un `setTimeout`."""

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
        """Nessuna delle quattro lo faceva: si chiudeva la finestra e il fuoco
        restava dentro un nodo che non esisteva piu'."""

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
        """«Sfoglia il listino» puo' sparire dopo che l'abbinamento a mano ha
        cambiato la scheda del prodotto: senza ripiego il fuoco resterebbe nel
        vuoto."""

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
        """Senza la chiave non c'e' niente da ricordare, e il fuoco
        ripiegherebbe sempre su `#workspace`."""

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
        # La quarta, che apre la finestra piu' consequenziale delle quattro:
        # decide in quale colonna del listino vengono scritte le quantita'.
        colonna = self.esegui(
            self.con_confronto(passo=1) + """
              const file = { name: "listino.xlsx", supplierId: "cipresso", kind: "Listino" };
              return renderColonnaDellOrdine(file, { orderColumn: { lettera: "P", intestazione: "Ordine" } });
            """,
        )
        self.assertIn('data-focus-key="colonna-ordine-cipresso"', colonna)


class LoSfondoChiudeLaFinestra(BancoDiProva):
    """Rilievo [6] dell'onda 5 — nessuna delle quattro finestre si chiudeva
    cliccando sullo sfondo, che e' il gesto che chiunque prova per primo.

    ⚠ Il clic sullo sfondo NON passa da `data-action`: `closest()` risalirebbe
    dal punto cliccato fino allo sfondo, e un clic dentro la finestra che
    finisce su un margine — fra due campi, accanto a un titolo — la
    chiuderebbe. Nella finestra della colonna d'ordine vorrebbe dire perdere la
    scelta appena fatta.

    Prove eseguite.
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
        """La trappola: un bersaglio che non e' lo sfondo non deve chiudere
        niente, nemmeno se lo sfondo e' un suo antenato."""

        aperta = self.esegui(
            self.con_confronto(passo=1) + """
              state.colonnaOrdine.aperta = true;
              cliccaSu("colonna-dialog");
              return state.colonnaOrdine.aperta;
            """,
        )
        self.assertTrue(aperta)

    def test_tutte_e_quattro_le_finestre_portano_lo_sfondo_che_il_gestore_cerca(self) -> None:
        """⚠ Senza questa, il gestore e il markup possono scollegarsi in
        silenzio: le altre prove della classe la classe se la fabbricano nel
        banco. Rinominandola nel solo markup la suite resterebbe verde e nel
        browser lo sfondo smetterebbe di chiudere le finestre — e perderebbe
        anche il velo scuro, che e' la stessa regola."""

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
    """Rilievo [4] dell'onda 5 — l'unico segnale di livello scheda che
    distingueva un avviso da un bloccante era il colore di una striscia
    laterale da 4 px: ambra contro rossa, 1,33:1 fra loro. Per chi ha una
    carenza sul rosso-verde, o su un monitor da negozio, erano la stessa
    striscia, e scorrendo venti schede non si individuava quella che ferma il
    lavoro. La riga dei metadati contava gia' i controlli, ma «3 controlli» si
    legge uguale che uno dei tre blocchi la compilazione o no.

    Prove eseguite: si disegna la scheda vera.
    """

    def scheda(self, *, bloccante: bool) -> str:
        # Il bloccante vero: una conferma richiesta e non data ferma la
        # compilazione. Un prodotto senza nessuna offerta NON la ferma — quella
        # e' una decisione presa il 16 agosto 2026 e scritta in collectIssues.
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
        """La regola CSS: niente piu' striscia laterale come unico segnale, ne'
        sulla scheda ne' sulla fascia dei documenti cambiati."""

        import re

        css = re.sub(r"\s+", " ", STYLES.read_text(encoding="utf-8"))

        # Nessuna striscia laterale colorata, comunque sia scritta.
        self.assertNotRegex(css, r"border-left: *[2-9]px +solid")
        # E il rinforzo che l'ha sostituita c'e' davvero: bordo pieno e fondo
        # tinto su tutt'e due gli stati.
        for regola in (".product-card.has-warning {", ".product-card.has-blocker {"):
            corpo = css.split(regola, 1)[1].split("}", 1)[0]
            self.assertIn("border-color:", corpo, regola)
            self.assertIn("background:", corpo, regola)


class IlConfrontoDiceDiQuantoUnFornitoreCostaDiPiu(BancoDiProva):
    """Rilievo [0] dell'onda 5.

    Il rilievo originale diceva che la pagina «non dice mai quale conviene», e
    non era vero: la riga preselezionata E' la piu' conveniente al pezzo, sta in
    cima perche' l'ordinamento e' crescente sul prezzo al pezzo, ed e' bordata
    di verde. Il difetto vero e' piu' stretto e resta reale: nessuna colonna
    diceva DI QUANTO gli altri costano di piu', quindi chi voleva sapere se
    cambiare fornitore valeva la pena doveva sottrarre a mente due numeri per
    ogni riga. E appena si sceglie a mano un fornitore piu' caro, il bordo verde
    si sposta sulla scelta e il piu' conveniente non e' piu' marcato da niente.

    Prove eseguite: si disegna la griglia vera.
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

        # 1,10 al pezzo contro 1,00: dieci centesimi.
        self.assertIn("+0,10\xa0€/pz", html)

    def test_il_piu_conveniente_non_porta_nessuna_differenza(self) -> None:
        """Aggiungergli «piu' conveniente» duplicherebbe un segnale gia' dato
        tre volte — e' primo, e' preselezionato, e' bordato di verde — e su una
        scelta manuale metterebbe due marcatori su righe diverse."""

        html = self.griglia()
        prima_riga = html.split('<label class="offer-card', 2)[1]
        # Le sole righe della tabella: la riga chiusa del riquadro dice
        # «hai il piu' conveniente al pezzo», ed e' un'altra cosa — parla della
        # scelta, non marca una riga.
        griglia = html.split('<div class="offer-grid">', 1)[1]

        self.assertIn("CIPRESSO", prima_riga)
        self.assertNotIn("offer-card__delta", prima_riga)
        self.assertNotIn("conveniente", griglia)

    def test_la_differenza_resta_anche_se_si_sceglie_a_mano_il_piu_caro(self) -> None:
        """E' il caso per cui il rilievo esiste: col bordo verde spostato sulla
        scelta, il piu' conveniente non era piu' marcato da niente."""

        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["selectedSupplierId"] = "betulla"
        html = self.griglia(revisione)

        self.assertIn("+0,10\xa0€/pz", html)

    def test_il_minimo_si_prende_fra_le_sole_offerte_disponibili(self) -> None:
        """⚠ Altrimenti su un prodotto dove la piu' economica non e'
        utilizzabile la pagina scrive differenze rispetto a un prezzo che
        nessuno puo' ordinare."""

        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["offers"].append(
            {"supplierId": "larice", "price": 12, "unitsPerOrderUnit": 24,
             "available": False, "warning": "Fuori assortimento."},
        )
        revisione["suppliers"].append({"id": "larice", "name": "LARICE", "minimumOrder": 0})
        html = self.griglia(revisione)

        # Il riferimento resta 1,00 (CIPRESSO), non 0,50 (LARICE, non disponibile).
        self.assertIn("+0,10\xa0€/pz", html)
        self.assertNotIn("+0,60\xa0€/pz", html)

    def test_due_offerte_allo_stesso_prezzo_non_scrivono_zero(self) -> None:
        revisione = json.loads(json.dumps(self.DUE_OFFERTE))
        revisione["products"][0]["offers"][1]["price"] = 24
        html = self.griglia(revisione)

        self.assertNotIn("+0,00", html)
        self.assertNotIn("offer-card__delta", html)

    def test_la_pagina_dice_una_volta_sola_com_e_ordinato_l_elenco(self) -> None:
        """Una volta per pagina, non sotto ogni griglia: il titolo «Confronto
        dei fornitori» stava su ogni scheda, venti volte per pagina, ed e' stato
        tolto per questo."""

        # ⚠ DUE prodotti: con uno solo, «una volta per pagina» e «una volta per
        # scheda» darebbero lo stesso conteggio e la prova non distinguerebbe
        # il difetto che dichiara di chiudere.
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
    """Rilievo [1] dell'onda 5 — i documenti rifiutati al caricamento vivevano
    3,6 secondi e non si diceva quali.

    Chi trascina cinque listini e ne ha uno in un formato non riconosciuto
    (.xlsm, .ods, .pdf) riceveva un messaggio a comparsa che spariva da solo,
    non nominava il file e non lasciava traccia da nessuna parte: i rifiutati
    non entravano in `state.pendingFiles`, quindi l'elenco sotto il riquadro
    mostrava solo gli accettati. La conseguenza possibile e' un confronto fatto
    senza un fornitore, cioe' un ordine mandato a chi costa di piu'.

    Prove eseguite: si chiama `addPendingFiles()` con file veri.
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
        """⚠ La prova che conta: `pendingFiles` e' l'array da cui parte la
        richiesta al servizio locale."""

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
        """⚠ `remove-file` passa l'indice dentro `state.pendingFiles`: riusarlo
        qui toglierebbe un documento buono al posto di uno scartato."""

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
        """Il caso peggiore: si trascina un solo file, ed e' quello sbagliato.
        Senza documenti in attesa `renderPendingFiles` usciva subito."""

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
    """Rilievo [3] dell'onda 5.

    L'avanzamento e' «fasi completate diviso nove», con tutte le fasi pesate
    uguali. `VALUTAZIONE_AI` e' la sesta e, per dichiarazione del codice stesso,
    «costa due minuti»: la barra sale a scatti fino al 55,6 %, poi resta
    immobile per il tempo piu' lungo dell'intero ricalcolo. Per una persona non
    tecnica «fermo al 56 % da due minuti» significa «si e' piantato», e la
    reazione naturale e' chiudere il programma proprio mentre la catena lavora.

    ⚠ Il tempo si conta dal timbro del servizio, non da un `Date.now()` fissato
    in pagina: un ricaricamento del browser a meta' ricalcolo farebbe ripartire
    il contatore da zero e direbbe il falso.

    Prove eseguite.
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
        """Fin li' la barra si muove ancora da sola: un contatore a zero
        sarebbe rumore."""

        html = self.disegna(0.3)

        self.assertNotIn("In corso da", html)

    def test_non_c_e_nessun_tetto_perche_e_li_che_serve(self) -> None:
        """«In corso da 47 minuti» e' esattamente la frase che serve quando
        qualcosa non va."""

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
    """Rilievo [9] dell'onda 5.

    «Compila i listini» crea copie di file su disco e registra una compilazione
    nello storico: e' l'unica azione della pagina che lascia un segno fuori dal
    programma. Prima di premere, la scheda diceva che cosa succede in generale e
    mostrava un totale, ma non quante copie ne' per quali fornitori — cioe' il
    numero di documenti che tra un attimo sarebbero esistiti. E dopo,
    `state.compilazioni.aperta` nasce `false`, quindi il riquadro che dice DOVE
    sono finiti restava chiuso, a un clic di distanza e senza che niente lo
    suggerisse.

    Prove eseguite.
    """

    def test_la_riga_dice_quante_copie_e_per_chi(self) -> None:
        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3),
        )

        # ⚠ Anche il participio va al singolare: `contati()` fa variare il
        # nome, non il verbo che gli sta davanti.
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
        """⚠ Solo conteggio e nomi. Da quel riquadro sono stati tolti ottanta
        avvisi duplicati proprio per non far leggere due volte la stessa cosa."""

        html = self.esegui(
            "return renderCompileStep();",
            preparazione=self.con_confronto(passo=3),
        )
        riga = html.split('class="compile-card__copie"', 1)[1].split("</p>", 1)[0]

        self.assertNotIn("€", riga)
        self.assertNotIn("riga", riga)


class LaSpuntaSuiPassiNonMentePiu(BancoDiProva):
    """Restyling — la spunta verde diceva «fatto» e voleva dire «ci sei
    passato».

    Era `state.currentStep > step.id`, cioe' un fatto di sola posizione:
    bastava premere «2. Scegli» — premibile appena il servizio risponde, anche
    con zero listini caricati — perche' il passo 1 si dipingesse di verde con la
    spunta senza che fosse stato importato niente. All'inverso, tornando dalla
    pagina 3 alla 1 i passi 2 e 3 perdevano la spunta pur avendo quantita' e
    fornitori scelti. Per una persona non tecnica il verde con la spunta E'
    l'affermazione che quel passo e' a posto.

    Nello stesso file c'era gia' il commento che spiega perche' il distintivo in
    alto NON e' verde finche' un confronto non esiste davvero: quella bugia era
    stata corretta li' e sopravviveva qui.

    Prove eseguite.
    """

    def passi(self, preparazione: str) -> list:
        """⚠ Si guarda il MARKUP, non `passoCompletato()`. Una prova che
        asserisce sulla funzione ausiliaria resta verde anche rimettendo in
        `renderStepper` la regola vecchia — verificato: con
        `state.currentStep > step.id` al suo posto passavano tutte e cinque, e
        l'intera suite con loro."""

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
        """La controprova del vecchio comportamento: con la regola di prima
        questo sarebbe stato [true, false, false] per la sola posizione."""

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
        """Prima, dalla pagina 3 alla 1, i passi 2 e 3 perdevano la spunta pur
        avendo quantita' e fornitori scelti."""

        avanti = self.passi(self.con_confronto(passo=3))
        indietro = self.passi(self.con_confronto(passo=1))

        self.assertEqual(avanti[:2], indietro[:2])

    def test_il_terzo_passo_non_dichiara_nessun_fatto(self) -> None:
        """Quello che si fa li' e' compilare, e la compilazione ha il suo
        riquadro: un «fatto» sul terzo passo non avrebbe niente da dichiarare."""

        fatti = self.passi(self.con_confronto(passo=3))

        self.assertFalse(fatti[2])


class UnSoloComandoPrimarioPerPagina(BancoDiProva):
    """Restyling — «un solo comando primario per pagina» e' una regola gia'
    scritta nel progetto, e in due punti non era rispettata.

    In Impostazioni i pulsanti blu erano due — «Salva la chiave» a meta' pagina
    e «Salva modello e limiti» in fondo — e nessuno dei due salva quello che
    salva l'altro. E la barra dei passi, che sta fuori da `#app`, continuava a
    dichiarare attiva la pagina da cui si era entrati: appena si scorre, l'unica
    cosa ferma sullo schermo diceva «Importa i dati» mentre si stava guardando
    l'elenco delle uguaglianze dichiarate.

    Prove eseguite.
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
        # E nessuno dei tre passi resta marcato come quello in cui sei.
        self.assertNotIn('aria-current="step"', esito["conImpostazioni"])
        # Chiuse, la voce sparisce e il passo torna a essere quello attivo.
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
    """Restyling — «Azzera le quantita' predefinite» era un comando di massa
    senza conferma, senza annullo e con un'etichetta che non diceva che cosa
    tocca.

    Il codice azzera solo i prodotti con `quantitySource === "gestionale"`,
    cioe' quelli mai toccati a mano — chi modifica una quantita' la fa diventare
    «utente». Ma l'etichetta non lo diceva: chi legge ha davanti un pulsante
    che, per quanto ne sa, gli cancella il lavoro di mezz'ora. E dopo averlo
    premuto non c'era ritorno, mentre nella stessa pagina escludere UN prodotto
    — l'azione meno grave — ha la sua fascia «Annulla». Il rischio era trattato
    al contrario della sua gravita'.

    Prove eseguite.
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
        """⚠ La provenienza torna «gestionale»: se tornasse «utente» il pulsante
        non le vedrebbe piu', e premerlo di nuovo non farebbe niente."""

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
        """La gemella della prova sul «×» del riepilogo: le quantita' salvate
        erano quelle del confronto di prima."""

        rimasto = self.esegui(
            self.con_confronto(passo=2, revisione=self.con_due_origini()) + """
              premi("reset-suggested-quantities");
              restoreExcludedProducts();
              return state.azzeramentoUndo;
            """,
        )

        self.assertIsNone(rimasto)

    def test_toccare_una_quantita_fa_scadere_la_fascia(self) -> None:
        """Premerla dopo aver rimesso mano ai numeri riscriverebbe quello che
        l'utente ha appena scritto. `goToStep()` invalida l'annullo dello
        spostamento fra fornitori esattamente per questa ragione."""

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
    """Restyling — «Annulla» significava due cose diverse nella stessa pagina.

    «Non fare quello che stavi per fare» sulle conferme di eliminazione e nelle
    finestre, e «disfa quello che ho appena fatto» sulle fasce di annullamento.
    Sono due gesti opposti — uno lascia le cose come stanno, l'altro le cambia —
    con la stessa parola sopra. Resta al primo, che e' quello che si trova in
    ogni programma; il secondo diventa «Rimetti», che dice anche CHE COSA
    rimette.

    Prove eseguite.
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
        """Li' «Annulla» è esatto: non fa niente, e lascia il documento dov'è."""

        html = self.esegui(
            self.con_confronto(passo=1) + """
              state.uploadDeletion.confirming = "gestionale.xlsx";
              return renderUploadStep();
            """,
        )

        self.assertIn('data-action="cancel-delete-upload"', html)
        self.assertIn(">Annulla<", html)

    def test_il_comando_che_esclude_si_legge_come_un_comando(self) -> None:
        """⚠ «Non ordinare» si leggeva come uno stato gia' deciso, non come un
        comando da premere — e la «✕» che glielo davanti, e che era l'unica
        cosa a dire «qui si preme», somigliava al «×» del riepilogo, che pero'
        fa un'altra cosa. Un verbo all'imperativo lo dice senza glifi, e
        «Escludi» e' la parola che il resto della pagina usa gia': il
        distintivo dice «Escluso» e il filtro dice «Esclusi»."""

        html = self.esegui(
            "return renderCompactProduct(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2),
        )

        self.assertIn("Escludi dall’ordine", html)
        self.assertNotIn("Non ordinare", html)
        self.assertNotIn("✕", html)


class UnaFinestraCheSiApreSiPrendeIlFuoco(BancoDiProva):
    """Il secondo sospetto della §18.7, guardato da vicino: **non è un difetto**.

    Il sospetto diceva «il fuoco potrebbe tornare sullo stesso campo col Tab».
    Il meccanismo esiste e si riproduce: `portaIlFuocoDentro` sposta il fuoco
    dopo un tick (`setTimeout(…, 0)`, perché `autofocus` non viene onorato sui
    nodi inseriti con `innerHTML`), e non guarda dove il fuoco sia finito nel
    frattempo. Chi premesse Tab dentro quel tick se lo vedrebbe tornare
    indietro.

    Ma è il comportamento **voluto**, ed è quello giusto: una finestra che si
    apre prende il fuoco, altrimenti chi usa la tastiera resta fuori da quello
    che è appena comparso. Le quattro chiamate stanno tutte all'apertura di una
    finestra — visualizzatore, colonna d'ordine, catalogo, spostamento fra
    fornitori — e nessuna dentro un ridisegno qualunque.

    La prova sta qui perché il giorno in cui qualcuno la togliesse, credendo di
    riparare quel sospetto, romperebbe l'accessibilità delle quattro finestre.
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
        """Le chiamate dichiarano sempre due selettori: il campo, e la «×» che
        chiude. Un campo che in quella finestra non esiste — il catalogo senza
        casella di ricerca — non deve lasciare il fuoco fuori."""

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
        """Se una lo perdesse, si aprirebbe una finestra da cui la tastiera non
        entra — e non se ne accorgerebbe nessuno finché non la usa qualcuno che
        col mouse non ci arriva."""

        sorgente = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        chiamate = sorgente.count("portaIlFuocoDentro([")

        self.assertEqual(chiamate, 4, "una delle quattro finestre non porta più il fuoco dentro")


class IlSiELIlNoSiDannoNelloStessoModo(BancoDiProva):
    """⚠ Una domanda sola, e fino al 22 agosto 2026 due meccaniche.

    Il sì era una **casella**: metteva un valore nello stato e aspettava il
    salvataggio differito — nessuna attesa dichiarata, nessun riscontro, e se il
    salvataggio falliva la spunta restava disegnata a dire che la risposta era
    data. Il no era un **pulsante**: agiva, aspettava e lo diceva. La forma
    giusta ce l'aveva già il gemello di questa scheda, «È lo stesso prodotto?
    Sì / No».

    Prove eseguite: `premi()` esegue i gestori veri, e il servizio finto
    risponde.
    """

    def revisione(self) -> dict:
        base = json.loads(json.dumps(REVISIONE))
        prodotto = base["products"][0]
        prodotto["requiresConfirmation"] = True
        prodotto["confirmed"] = False
        prodotto["selectedSupplierId"] = "cipresso"
        return base

    def test_il_si_parte_subito_e_non_aspetta_il_salvataggio_differito(self) -> None:
        """⚠ La casella marcava e basta: la risposta partiva 450 ms dopo, se
        nel frattempo non succedeva altro. Qui si aspetta e si dichiara."""

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
        """⚠ È il difetto che la casella aveva e non poteva non avere: una
        spunta disegnata sopra una risposta che non è arrivata da nessuna parte.
        Chi la guarda crede di aver risposto, e alla compilazione si ritrova
        «Conferma richiesta · bloccante» senza capire perché."""

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
            # 500: il servizio c'e' e rifiuta. E' il caso che la casella non
            # sapeva mostrare — restava spuntata sopra un nulla di fatto.
            risposte={"/api/state": {"__stato": 500, "message": "Il servizio non risponde"}},
        )

        self.assertFalse(esito["confermato"], "il sì non deve restare disegnato")

    def test_mentre_il_si_va_i_due_pulsanti_dicono_che_stanno_lavorando(self) -> None:
        html = self.esegui(
            "state.matches.answering = 'p1:conferma'; return renderConfirmation(state.review.products[0]);",
            preparazione=self.con_confronto(passo=2, revisione=self.revisione()),
        )

        self.assertIn("Attendere…", html)
        # E il no è spento: una risposta per volta alla stessa domanda.
        self.assertIn("disabled", html.split('data-action="rifiuta-abbinamento"', 1)[1].split(">", 1)[0])

    def test_le_due_risposte_sono_due_pulsanti_gemelli(self) -> None:
        """La casella non c'è più: due modi di dare la stessa risposta sono la
        cosa che questa correzione esiste per togliere."""

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
    """Il comando che apre il selettore delle colonne, e la schermata che apre.

    ⚠ «Prima avevo il selettore manuale dove potevo confermare quale colonna
    contenesse quale dato, con anteprima, mentre ora non lo vedo più» (Daniele,
    15 agosto 2026). Non era sparito: si apre solo quando la catena si ferma su
    uno schema sconosciuto, e con i fornitori riconosciuti non si ferma mai —
    non c'era più nessuna porta per arrivarci.

    Il comando sta **dentro il riquadro «Quali colonne leggo»**, e non fra le
    azioni del documento: quello è il posto in cui si stanno già guardando le
    colonne, e chi vede una riga sbagliata la corregge da dove l'ha vista.
    Prove eseguite.
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
        # Dentro il riquadro, non fra le azioni del documento: se finisse
        # accanto a «Elimina» sarebbe un comando in mezzo a quelli distruttivi.
        dentro = html.split('class="doc-columns"', 1)[1].split("</details>", 1)[0]
        self.assertIn('data-action="apri-colonne"', dentro)

    def test_e_il_comando_e_piccolo_e_discreto(self) -> None:
        """«Tasto discreto ma chiaro, piccolo»: non compete con «Continua»."""

        html = self.scheda()
        comando = html.split('data-action="apri-colonne"', 1)[0]

        self.assertIn("button--ghost", comando[-260:])
        self.assertIn("button--piccolo", comando[-260:])
        self.assertNotIn("button--primary", comando[-260:])

    def test_mentre_il_confronto_gira_il_comando_e_spento(self) -> None:
        """Cambiare le colonne mentre la catena legge quel file non ha senso."""

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
        """Una tabella di anteprima in mezzo alle schede dei file non si legge."""

        html = self.pagina('state.schemaMapping.documento = "LISTINO BETULLA.xlsx";')

        self.assertIn("Colonne del listino", html)
        self.assertIn("LISTINO BETULLA.xlsx", html)
        self.assertIn('data-action="chiudi-colonne"', html)
        # E l'elenco dei documenti non c'è: è un lavoro per volta.
        self.assertNotIn('data-action="apri-colonne"', html)

    def test_e_dice_che_cosa_costa_cambiarle(self) -> None:
        """⚠ Cambiare una colonna **letta** cambia i prezzi, quindi il confronto
        va rifatto. Un selettore che non lo dice fa credere che salvare basti."""

        html = self.pagina('state.schemaMapping.documento = "LISTINO BETULLA.xlsx";')

        self.assertIn("cambia i prezzi del confronto", html)

    def test_i_comandi_del_selettore_non_fanno_ripartire_il_confronto(self) -> None:
        """Dieci minuti non si prendono senza che l'utente li abbia chiesti."""

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
        """Il documento è lo stesso: a cambiare è come lo si legge."""

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
    """Detto l'ultimo «non è lo stesso articolo», il prodotto sparisce da sotto
    il dito: esce da «Da confermare», perché non è più una domanda.

    ⚠ La frase che dice dove è andato prometteva il filtro **sbagliato**:
    mandava a «Nessuno ce l’ha», che dal 22 agosto 2026 quel prodotto non lo
    contiene più — su di lui quel nome diceva il falso, i listini la riga ce
    l'avevano. Mandare a cercare nel posto sbagliato è peggio che tacere.
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
        # E dice che la quantità è ancora lì: è il numero che serve a reperirlo.
        self.assertIn("quantità", frase)

    def test_se_resta_un_fornitore_non_dice_niente(self) -> None:
        """Un prodotto che si può ancora ordinare non è andato da nessuna parte."""

        prodotto = self.prodotto_scartato()
        prodotto["offers"].append(
            {"supplierId": "betulla", "price": 10, "unitsPerOrderUnit": 6, "available": True},
        )

        self.assertEqual(self.frase(prodotto), "")

    def test_dopo_un_si_non_dice_niente(self) -> None:
        self.assertEqual(self.frase(self.prodotto_scartato(), rifiutata=False), "")


class IlPercheDelDocumentoInMappaturaGuidata(BancoDiProva):
    """La frase in cima al documento nella mappatura guidata.

    ⚠ Il 21 agosto 2026 un listino BETULLA risalvato in Excel — la cella C1
    svuotata, la parola ORDINE sparita — e' arrivato qui con «questo non lo
    conosco», e chi l'ha letto ha configurato BETULLA come un fornitore nuovo,
    scrivendo un adattatore imparato sopra quello spedito. Il registro sapeva
    di chi era il documento e sapeva che cosa gli mancava.

    Prove eseguite: si chiama la funzione vera di `app.js`.
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
        # Le due cose che deve sapere chi legge: da dove viene il guasto...
        self.assertIn("Excel", html)
        # ...e che la mossa giusta non e' configurarlo qui.
        self.assertIn("ricaricare l’originale", html)

    def test_senza_il_nome_del_fornitore_non_si_inventa_niente(self) -> None:
        """Meglio la frase di prima che una frase nuova a metà."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({
                "state": "QUASI", "supplierName": "",
                "missing": [{"header": "ORDINE", "column": "C"}], "present": 4,
            })) + ");",
        )

        self.assertEqual(html, "")

    def test_il_documento_sconosciuto_resta_senza_frase(self) -> None:
        """Un documento che il registro non conosce davvero non deve
        cominciare a somigliare a qualcuno."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({"state": "SCONOSCIUTO"})) + ");",
        )

        self.assertEqual(html, "")

    def test_il_listino_cambiato_dice_ancora_la_sua_frase(self) -> None:
        """La frase del VARIATO non deve essere stata mangiata da quella nuova."""

        html = self.esegui(
            "return renderPercheEQui(" + json.dumps(self.documento({
                "state": "VARIATO", "supplierName": "CIPRESSO",
                "changed": ["il nome del foglio"],
            })) + ");",
        )

        self.assertIn("CIPRESSO", html)
        self.assertIn("il nome del foglio", html)


# --- Le tre cose sulla pagina 3, chieste il 22 agosto 2026 -------------------


class LaPaginaTreSiLeggeMentreLavora(BancoDiProva):
    """Le tre richieste del §19 di «Lavori aperti», provate eseguite.

    Nessuna delle tre tocca un numero: sono su come si legge la pagina 3 e su
    che cosa si capisce mentre il programma lavora.
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

    # --- 19.1 l'elenco per fornitore si richiude ----------------------------

    def test_l_elenco_di_un_fornitore_nasce_aperto(self) -> None:
        """Come si legge oggi: chiudere di suo nasconderebbe quello che si e'
        venuti a guardare."""

        html = self.pagina()

        self.assertIn('<details class="supplier-summary" data-ricorda="riepilogo-fornitore:cipresso" open', html)
        self.assertIn('<details class="supplier-summary" data-ricorda="riepilogo-fornitore:betulla" open', html)

    def test_il_nome_del_fornitore_e_il_comando_che_chiude(self) -> None:
        """Il gesto chiesto: si clicca sul nome, non su una freccina a parte."""

        html = self.pagina()
        testata = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</summary>", 1)[0]

        self.assertIn('<summary class="supplier-summary__head">', testata)
        self.assertIn("CIPRESSO", testata)

    def test_chiuso_resta_visibile_il_subtotale(self) -> None:
        """«Lasciando visibile il suo subtotale»: il totale sta nel `<summary>`,
        cioe' nella parte che non si chiude. Le righe dei prodotti no."""

        html = self.pagina()
        testata = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</summary>", 1)[0]
        corpo = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split("</details>", 1)[0]

        self.assertIn("supplier-summary__total", testata)
        self.assertNotIn("Prodotto uno", testata)
        self.assertIn("Prodotto uno", corpo)

    def test_chiuso_da_chi_ordina_non_si_riapre_al_ridisegno(self) -> None:
        """Il difetto che il §19 chiedeva di non introdurre: il riepilogo si
        rifa' a ogni cambio di quantita', e senza memoria del «chiuso» tornerebbe
        aperto sotto le dita."""

        due = self.esegui(
            "const finto = { dataset: { ricorda: 'riepilogo-fornitore:cipresso' }, open: false };"
            "ricordaApertura(finto);"
            "return [renderCompileStep(), renderCompileStep()];",
            preparazione=self.con_confronto(pipeline=None, revisione=self.DUE_FORNITORI, passo=3),
        )

        for html in due:
            apertura = html.split('data-ricorda="riepilogo-fornitore:cipresso"', 1)[1].split(">", 1)[0]
            self.assertNotIn("open", apertura)
            # E l'altro fornitore, che nessuno ha toccato, resta aperto.
            self.assertIn("open", html.split('data-ricorda="riepilogo-fornitore:betulla"', 1)[1].split(">", 1)[0])

    def test_due_fornitori_non_si_chiudono_insieme(self) -> None:
        """La chiave porta dentro l'identificativo del fornitore."""

        html = self.pagina()

        self.assertIn("riepilogo-fornitore:cipresso", html)
        self.assertIn("riepilogo-fornitore:betulla", html)

    def test_raggruppando_per_prodotto_non_si_chiude_niente(self) -> None:
        """La richiesta riguarda **solo** il raggruppamento per fornitore: la
        stessa classe, li', sta su un riquadro che non apre niente."""

        html = self.pagina(preparazione_extra='state.summary.grouping = "product";')

        self.assertIn('<article class="supplier-summary">', html)
        self.assertNotIn("riepilogo-fornitore:", html)

    # --- 19.2 i totali per fornitore anche in pagina 3 ----------------------

    def test_la_fascia_dei_totali_c_e_anche_in_pagina_tre(self) -> None:
        """È lo stesso componente della pagina 2, non una copia."""

        html = self.pagina()
        fascia = html.split('class="supplier-totals-strip"', 1)[1].split("</aside>", 1)[0]

        self.assertIn('aria-label="Totali ordine per fornitore"', html)
        self.assertIn("CIPRESSO", fascia)
        self.assertIn("BETULLA", fascia)
        self.assertIn("Totale", fascia)

    def test_la_fascia_sta_in_cima_prima_di_tutto_il_resto(self) -> None:
        """Agganciata in alto, e sopra a quello che scorre: se finisse in fondo
        alla pagina l'aggancio non servirebbe a niente."""

        html = self.pagina()

        self.assertLess(html.index("supplier-totals-strip"), html.index("Ordini da preparare"))
        self.assertLess(html.index("supplier-totals-strip"), html.index("Compilazione dei listini"))

    def test_la_fascia_e_l_unica_cosa_agganciata_in_alto_oltre_ai_passi(self) -> None:
        """L'avvertenza del §19.2: in pagina 3 c'e' gia' altro contenuto in
        cima, e due elementi agganciati alla stessa quota si sovrappongono.
        Le quote dichiarate nel foglio restano due."""

        foglio = STYLES.read_text(encoding="utf-8")
        regole = re.findall(r"([^{}]+)\{([^{}]*position:\s*sticky[^{}]*)\}", foglio)
        # Chi si aggancia a una quota diversa da zero si mette SOTTO qualcosa
        # d'altro: sono quelle le regole che possono sovrapporsi fra loro.
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
        # E quello a cui si aggancia — la barra dei passi — sta a zero.
        self.assertIn("top: 0", foglio.split(".stepper-shell {", 1)[1].split("}", 1)[0])

    # --- 19.3 «Compila i listini» mostra che sta lavorando ------------------

    def test_mentre_compila_si_vede_che_sta_lavorando(self) -> None:
        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertIn("compile-progress__bar", html)
        self.assertIn('role="progressbar"', html)
        self.assertIn("Sto scrivendo", html)

    def test_la_barra_non_c_e_quando_non_sta_compilando(self) -> None:
        """Una barra ferma a riposo direbbe il falso, e a compilazione finita
        direbbe «sto ancora lavorando»."""

        html = self.pagina()

        self.assertNotIn("compile-progress", html)

    def test_la_barra_non_promette_una_percentuale_che_non_ha(self) -> None:
        """⚠ Il writer Node non riporta avanzamenti: qualunque numero qui
        sarebbe inventato. `aria-valuenow` assente e' il modo dichiarato di
        dire «indeterminata»."""

        html = self.pagina(preparazione_extra="state.compiling = true;")
        barra = html.split("compile-progress__bar", 1)[1].split(">", 1)[0]

        self.assertNotIn("aria-valuenow", barra)
        self.assertNotIn("%", barra)
        self.assertNotIn("width:", barra)

    def test_la_barra_sta_sotto_il_pulsante_che_si_e_premuto(self) -> None:
        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertLess(html.index("Compilazione in corso…"), html.index("compile-progress"))

    def test_chi_ha_chiesto_meno_movimento_legge_la_frase(self) -> None:
        """Sotto `prefers-reduced-motion` la regola generale azzera le durate:
        una barra che restasse resterebbe immobile, che e' proprio la cosa che
        si legge come «si e' piantato». Sparisce la barra, resta la frase."""

        foglio = STYLES.read_text(encoding="utf-8")
        ridotto = foglio.split("@media (prefers-reduced-motion: reduce) {")

        self.assertTrue(any(".compile-progress__bar" in blocco and "display: none" in blocco
                            for blocco in ridotto[1:]))

    def test_quante_copie_si_stanno_scrivendo(self) -> None:
        """Il numero c'e' e viene dai fornitori veri, e il plurale e' quello
        giusto: `contati`, non una «s» attaccata."""

        html = self.pagina(preparazione_extra="state.compiling = true;")

        self.assertIn("2 copie", html)


# --- «Inizia nuova comparazione» in pagina 1, 22 agosto 2026 ----------------


class IlPrezzoAlPezzoInPaginaTre(BancoDiProva):
    """Accanto al totale di ogni riga del riepilogo, in grigio, il prezzo di un
    pezzo, sotto un'intestazione che dice che cos'e' (richiesta di Daniele, 21
    settembre 2026).

    E' lo stesso numero della colonna «Prezzo per pezzo» di pagina 2, quello con
    cui il fornitore e' stato scelto, e non entra in nessun totale.
    """

    # CIPRESSO 10 € il collo da 6 (1,67 € al pezzo), BETULLA 4 € il collo da 3 (1,33 €).
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

        # `aria-hidden` come `.offer-grid__head`: al lettore di schermo la dice
        # la parola «al pezzo» accanto a ogni numero.
        intestazione = ('<li class="order-lines__intestazione" aria-hidden="true"><span>Prodotto</span><span>Quantità</span>'
                        '<span></span><span>Prezzo al pezzo</span><span>Totale</span></li>')
        self.assertEqual(html.count(intestazione), 2, "una per fornitore")

    def test_per_prodotto_anche(self) -> None:
        html = self.pagina(extra='state.summary.grouping = "product";')

        self.assertIn("<span>Fornitore</span><span>Quantità</span><span></span><span>Prezzo al pezzo</span><span>Totale</span>", html)
        self.assertRegex(self.riga(html, "Prodotto uno"), r'1,67\xa0€.*\s*<strong>30,00\xa0€</strong>')
        self.assertRegex(self.riga(html, "Prodotto due"), r'1,33\xa0€.*\s*<strong>8,00\xa0€</strong>')

    def test_e_il_prezzo_del_listino_non_il_collo_diviso_i_pezzi(self) -> None:
        """Caso vero: KIF CANDEGGINA SPRAY 650ML, LARICE, 23 agosto 2026.
        16,74 / 12 fa 1,3949999… e si scriveva 1,39 sotto il totale di pagina 2,
        mentre la colonna accanto — e il listino, 1,395 — dicevano 1,40."""

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
        """Il prezzo al pezzo si legge e basta: totali di fornitore e d'ordine
        sono quelli di prima."""

        html = self.pagina()

        self.assertIn("<strong>30,00\xa0€</strong>", html)
        self.assertIn("<strong>8,00\xa0€</strong>", html)
        self.assertEqual(self.esegui("return allOrderTotal();", preparazione=self.con_confronto(
            pipeline=None, revisione=self.REVISIONE, passo=3)), 38)


class IniziaNuovaComparazione(BancoDiProva):
    """Il comando che apre la settimana, provato eseguito.

    Il giro di ogni lunedì era togliere l'elenco del gestionale e poi i listini
    uno per uno. E chi lo saltava non se ne accorgeva: due listini dello stesso
    fornitore ne fanno entrare in confronto **uno solo**, scelto sulla data di
    modifica del file.
    """

    SENZA_CONFRONTO = {**REVISIONE, "files": [], "products": []}

    def pagina(self, *, revisione: dict | None = None, preparazione_extra: str = "") -> str:
        return self.esegui(
            f"{preparazione_extra} return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=revisione or REVISIONE, passo=1),
        )

    # -- il comando ----------------------------------------------------------

    def test_il_comando_sta_in_alto_nella_testata(self) -> None:
        """Non fra i comandi di caricamento: non è un modo di caricare un
        documento, è il gesto con cui si apre la settimana."""

        html = self.pagina()
        testata = html.split("page-heading__meta", 1)[1].split("</div>", 1)[0]

        self.assertIn('data-action="nuova-comparazione"', testata)
        self.assertIn("Inizia nuova comparazione", testata)
        # E resta accanto a Impostazioni, che è l'altro comando di servizio.
        self.assertIn('data-action="apri-impostazioni"', testata)

    def test_senza_niente_da_svuotare_il_comando_e_spento(self) -> None:
        """Un comando che non farebbe niente non deve rispondere «fatto» al
        vuoto."""

        html = self.pagina(revisione=self.SENZA_CONFRONTO)
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", comando)

    def test_con_documenti_il_comando_e_premibile(self) -> None:
        html = self.pagina()
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertNotIn("disabled", comando)

    def test_mentre_il_confronto_gira_il_comando_e_spento(self) -> None:
        """La stessa regola dei caricamenti: non si cambiano i documenti sotto
        una catena che li sta leggendo."""

        html = self.esegui(
            "return renderUploadStep();",
            preparazione=self.con_confronto(
                pipeline={"stato": "IN_CORSO", "fasi": []}, revisione=REVISIONE, passo=1,
            ),
        )
        comando = html.split('data-action="nuova-comparazione"', 1)[1].split(">", 1)[0]

        self.assertIn("disabled", comando)

    # -- la conferma ---------------------------------------------------------

    def test_prima_di_chiedere_non_si_vede_nessuna_conferma(self) -> None:
        html = self.pagina()

        self.assertNotIn("conferma-riga", html)

    def test_la_conferma_dice_quanti_documenti_toglie(self) -> None:
        """Il numero è quello delle schede che si vedono sopra: si controlla a
        occhio. ⚠ Non si conta per `role`, che `normalizeReview` non copia —
        filtrando per ruolo il conto usciva zero con quattro documenti in
        pagina."""

        html = self.pagina(preparazione_extra="state.nuovaComparazione.chiedendo = true;")
        riga = html.split("conferma-riga", 1)[1].split("</div>", 1)[0]

        self.assertIn("2 documenti caricati", riga)

    def test_la_conferma_dice_che_cosa_resta(self) -> None:
        """È la metà che conta: la paura di chi legge non è quello che il
        comando toglie, è quello che non sa se toglie."""

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
        """I due rami del gestore, premuti davvero."""

        esito = self.esegui(
            'premi("nuova-comparazione");'
            "const aperta = state.nuovaComparazione.chiedendo;"
            'premi("annulla-nuova-comparazione");'
            "return { aperta, chiusa: state.nuovaComparazione.chiedendo };",
            preparazione=self.con_confronto(pipeline=None, revisione=REVISIONE, passo=1),
        )

        self.assertTrue(esito["aperta"])
        self.assertFalse(esito["chiusa"])

    # -- la domanda sugli ordini della settimana scorsa ----------------------

    def test_senza_confronto_la_domanda_sugli_ordini_sta_in_pagina_1(self) -> None:
        """Il momento in cui ci si chiede «è arrivata?» è quello in cui si
        comincia la comparazione nuova, non a metà della pagina 2."""

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
        """A confronto caricato la pagina 1 è quella dei documenti: la domanda
        non ci si mette in mezzo, e resta in pagina 2 come sempre."""

        html = self.esegui(
            'state.history.pending = [{ orderId: "o1", supplier: "betulla", supplierName: "BETULLA",'
            ' createdAt: "2026-08-17T10:00:00+02:00", lineCount: 12, totalNet: 120 }];'
            "return renderUploadStep();",
            preparazione=self.con_confronto(pipeline=None, revisione=REVISIONE, passo=1),
        )

        self.assertNotIn("pending-orders", html)



class RimetteNellOrdineRimetteLaQuantita(BancoDiProva):
    """«Rimetti nell'ordine» rimetteva il prodotto e lasciava la quantità a zero.

    Le due strade per disfare un'esclusione facevano due cose diverse: la
    fascia «… è stato escluso. [Rimetti nell'ordine]» rimetteva quantità,
    fornitore e conferma; il pulsante sulla scheda del prodotto — che è l'unico
    che resta quando la fascia è scaduta, e l'unico dopo un ricaricamento —
    toglieva soltanto l'esclusione. Il prodotto tornava nell'elenco, la pagina
    diceva «Puoi ripristinarlo in qualsiasi momento», e non veniva ordinato.

    Prove eseguite.
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
        """La fascia è UNA sola: la seconda esclusione cancella l'annullo della prima."""

        esito = self.esegui(
            self.con_confronto(passo=2, revisione=self.revisione_con_due_prodotti()) + """
              premi("exclude-product", { productId: "p1" });
              premi("exclude-product", { productId: "p2" });
              premi("restore-product", { productId: "p1" });
              const prodotto = findProduct("p1");
              return { quantita: orderQuantity(prodotto), annullo: state.exclusionUndo && state.exclusionUndo.id };
            """,
        )
        # L'annullo della fascia parla del secondo prodotto…
        self.assertEqual(esito["annullo"], "p2")
        # …e il primo torna lo stesso con la sua quantità.
        self.assertEqual(esito["quantita"], 3)

    def test_torna_anche_dopo_un_ricaricamento_della_pagina(self) -> None:
        """È il caso vero: si esclude oggi e ci si ripensa domani mattina.

        Il ricaricamento si mette in scena come lo fa il programma: si rilegge
        il confronto dal servizio — dove il prodotto escluso ha quantità zero,
        perché è così che il servizio lo salva — e si richiamano gli esclusi
        dalla memoria del browser.
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
        """Chi aggiorna con dei prodotti già esclusi ha ancora la forma di prima.

        Allora era un elenco di identificativi e basta: di quei prodotti la
        quantità di prima non la sa nessuno, e «Rimetti nell'ordine» fa quello
        che faceva — rimette il prodotto e lascia la quantità dov'è. Quello che
        non deve fare è rompersi.
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
    """Quattro modi di premere il comando sbagliato al momento sbagliato.

    Tutti e quattro hanno la stessa forma: la pagina lascia fare una cosa che
    non si può fare adesso, e il danno si vede dopo — un confronto sui listini
    della settimana prima, un ordine compilato con i prezzi vecchi, un errore
    che dice il falso, un collegamento che sparisce.

    Prove eseguite.
    """

    def test_confronta_e_spento_se_ci_sono_documenti_scelti_e_non_caricati(self) -> None:
        """Il tranello del lunedì: si sceglie il listino nuovo e non si carica."""

        comando = self.esegui("return comandoConfronto();",
                              preparazione=self.con_confronto(pendenti=1))
        self.assertTrue(comando["disabilitato"])
        self.assertIn("caricali con il comando qui sopra", comando["nota"].lower())

    def test_senza_documenti_scelti_il_comando_torna(self) -> None:
        """La controprova: non è il pulsante a essere spento per sempre."""

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
        """Su questo pulsante il doppio clic è il gesto più naturale del mondo.

        Non succede niente per un secondo, e si preme di nuovo. Prima partivano
        due richieste: il servizio avviava la prima e rispondeva 409 alla
        seconda, e la pagina diceva «Il confronto non è partito» mentre il
        confronto stava partendo.
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
        """⚠ Spariva «Scarica i listini» nel momento in cui il lavoro era finito."""

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
        # Cambiare passo invece lo azzera, come prima: quell'esito parla della
        # pagina 3, e tornandoci si ricarica lo storico delle compilazioni.
        self.assertIsNone(esito["dopoUnAltro"])



class LeRisposteVecchieNonVincono(BancoDiProva):
    """Due letture in volo, e la pagina applicava quella che arrivava per ultima.

    Succede a chi scrive nella ricerca del visualizzatore mentre la prima
    lettura è ancora in corso, e a chi apre «Cambia colonna» su un fornitore,
    la chiude e ne apre un'altra. In tutti e due i casi il danno non è
    estetico: dalla tabella del visualizzatore si preme «È questo», che scrive
    un abbinamento permanente in `conferme.db`, e dalla finestra delle colonne
    si preme «Scrivi l'ordine qui», che manda al servizio un fornitore e un
    numero di colonna.

    Prove eseguite.
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
        # E non spegne nemmeno l'attesa: quella la spegnerà la lettura vera.
        self.assertTrue(esito["caricando"])

    def test_la_lettura_piu_recente_invece_scrive(self) -> None:
        """La controprova: non è che non applichi più niente."""

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
    """«Torna ai documenti» azzerava tutto quello che si era impostato.

    Dentro «Rivedi le colonne» si compilano foglio, riga d'intestazione, riga
    dei dati e una colonna per ogni campo — dieci campi guardando il file — e
    quel comando li buttava via in silenzio, con un'etichetta che prometteva
    soltanto di tornare indietro.

    Prove eseguite.
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
    """⚠ Il pulsante c'era nel codice, la prova era verde, e in pagina non c'era.

    Trovato usando il programma nel browser il 23 agosto 2026, con il servizio
    vero: sulla scheda del listino BETULLA compariva la riga «L'ordine viene
    scritto nella colonna C («ORDINE»)» e **non** il pulsante «Cambia colonna»
    che sta nello stesso template. `renderColonnaDellOrdine` lo disegna solo se
    `file.supplierId` c'è, e `normalizeReview` quel campo non lo copiava: il
    servizio lo manda, la pagina lo buttava via.

    La prova che c'era chiamava `renderColonnaDellOrdine({supplierId: "betulla"},
    …)` con un oggetto costruito a mano, cioè provava il template e non la
    strada. Queste partono da `normalizeReview`, come la pagina.
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
        """La riga che mancava, provata da sola: senza, il pulsante non nasce."""

        fornitore = self.esegui("return state.review.files[0].supplierId;",
                                preparazione=self.con_documento())

        self.assertEqual(fornitore, "betulla")

    def test_sul_gestionale_non_c_e_niente_da_spostare(self) -> None:
        """La controprova: l'elenco del gestionale non ha una colonna d'ordine."""

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
