/*
 * REST contract
 *
 * GET  /api/review
 *   -> { run, files, suppliers, products, warnings }
 *
 * POST /api/upload
 *   <- { files: [{ name, data }] } where data is base64 with no data: prefix
 *   -> { files, status, message }
 *
 * PUT  /api/state
 *   <- { runId, currentStep, summaryGrouping, products: [{ id, quantity, selectedSupplierId, confirmed, excluded }] }
 *
 * GET  /api/products/search and POST /api/products/add
 *   -> search and add products from the current catalogs
 *
 * GET  /api/history/pending
 *   -> { ok, pending: [{ orderId, supplier, supplierName, createdAt,
 *                        answeredAt, askAgainAt, lineCount, totalNet }] }
 *   Only orders already compiled and not yet confirmed as received.
 *
 * POST /api/history/answer
 *   <- { orderId, received } or { orderId, closed: true }
 *      received=true = received; received=false = ask again in seven days;
 *      closed = will not be asked again. Always for the whole order.
 *   -> { ok, pending: [...] }  updated list
 *
 * POST /api/matches/answer
 *   <- { runId, productId, supplierId, candidateKey, accepted }
 *   Confirms or rejects a proposed match, scoped to the current comparison run.
 *
 * POST /api/uploads/elimina
 *   <- { name }  deletes a single uploaded price-list copy
 *
 * POST /api/ordini/elimina
 *   <- { cartella }  deletes a compilation and its related reminders
 *
 * POST /api/suppliers/move-preview
 *   <- same snapshot as /api/state plus { from: "<source supplier id>" }
 *   -> { ok, from, fromName, movableCount, currentNetTotal, options: [{
 *          id, kind, label, movedCount, movableCount, deltaNet,
 *          assignments: [{ productId, productName, toSupplierId, previousFactor,
 *                          newFactor, factorChanged, needsConfirmation,
 *                          previousLineNet, newLineNet }],
 *          leftBehind: [{ productId, productName, reason }],
 *          supplierTotalsAfter: [{ supplierId, supplierName, netTotalBefore,
 *                                  netTotalAfter, threshold,
 *                                  meetsThresholdBefore, meetsThresholdAfter }] }] }
 *   Preview only: writes nothing and does not change the order. The actual
 *   move goes through PUT /api/state like any other edit.
 *
 * POST /api/compile
 *   <- same snapshot as /api/state
 *   -> { ok, message, cartella, zipUrl, zipNome, outputs: [{ name, url, tipo }] }
 *   zipUrl is null when the compilation produced no price list.
 *
 * GET  /api/ordini
 *   -> { ok, compilazioni: [{ cartella, etichetta, creatoIl, stato,
 *          fornitori: [{ id, nome, totaleNetto }], totaleNetto, righe, listini,
 *          file: [{ nome, tipo, url }], zipUrl, zipNome, completa }] }
 *   Newest first. `completa: false` means that compilation's audit trail
 *   can't be read back: the folder and its files are still there and
 *   still downloadable.
 *
 * Demo data is used only with ?demo=1.
 */

const STEPS = [
  { id: 1, label: "Importa i dati" },
  { id: 2, label: "Scegli prodotti e fornitori" },
  { id: 3, label: "Riepilogo e compilazione" },
];

const configuredApi = globalThis.COMPARATORE_CONFIG?.api || {};
const API = {
  review: configuredApi.review || "/api/review",
  upload: configuredApi.upload || "/api/upload",
  state: configuredApi.state || "/api/state",
  salute: configuredApi.salute || "/api/health",
  compile: configuredApi.compile || "/api/compile",
  productSearch: configuredApi.productSearch || "/api/products/search",
  productAdd: configuredApi.productAdd || "/api/products/add",
  // Full recompute of the comparison: one button, the pipeline runs on its own.
  pipelineAvvia: configuredApi.pipelineAvvia || "/api/pipeline/avvia",
  pipelineStato: configuredApi.pipelineStato || "/api/pipeline/stato",
  schemasPending: configuredApi.schemasPending || "/api/schemas/pending",
  schemasValidate: configuredApi.schemasValidate || "/api/schemas/validate",
  schemasConfirm: configuredApi.schemasConfirm || "/api/schemas/confirm",
  // Columns of an already-uploaded price list, editable on demand: without
  // these three, the mapping was only visible when the pipeline stalled.
  colonneDocumento: configuredApi.colonneDocumento || "/api/schemas/documento",
  colonneProva: configuredApi.colonneProva || "/api/schemas/documento/prova",
  colonneSalva: configuredApi.colonneSalva || "/api/schemas/documento/salva",
  // Which columns the current comparison actually read from each document.
  // Not the guided mapping (shown only when a file isn't recognized): this
  // is what happened on files that were recognized.
  schemasColumns: configuredApi.schemasColumns || "/api/schemas/columns",
  historyPending: configuredApi.historyPending || "/api/history/pending",
  historyAnswer: configuredApi.historyAnswer || "/api/history/answer",
  matchAnswer: configuredApi.matchAnswer || "/api/matches/answer",
  // A supplier's price list as the app parsed it, and manual matches made
  // on one of its rows.
  listino: configuredApi.listino || "/api/listino",
  matchAbbina: configuredApi.matchAbbina || "/api/matches/abbina",
  // "Not the same item": rejects a supplier's proposed match for a product.
  // Its own endpoint, same as accepting a match, and for the same reason:
  // it's an answer, not a quantity, and must be recorded in memory that
  // survives a recompute.
  matchRifiuta: configuredApi.matchRifiuta || "/api/matches/rifiuta",
  // Manual "these two codes are the same item" declarations. Its own endpoint,
  // outside the comparison: read back from Settings, which opens even without
  // an active comparison.
  uguaglianze: configuredApi.uguaglianze || "/api/matches/uguaglianze",
  uguaglianzaTogli: configuredApi.uguaglianzaTogli || "/api/matches/uguaglianze/togli",
  // Exports every confirmed match as a downloadable file. `conferme.db` is
  // the app's permanent memory with no built-in backup; this is the only way
  // to get one without copying an open SQLite file by hand.
  confermeEsporta: configuredApi.confermeEsporta || "/api/conferme/esporta",
  // The column the app writes ordered quantities into. Read and changed at
  // the same endpoint: GET lists the candidate columns, POST switches it —
  // and the writability check that gates this is the same one that gates
  // compilation, so a failure here is the real one.
  colonnaOrdine: configuredApi.colonnaOrdine || "/api/schemas/order-column",
  uploadDelete: configuredApi.uploadDelete || "/api/uploads/elimina",
  comparazioneNuova: configuredApi.comparazioneNuova || "/api/comparazione/nuova",
  supplierMovePreview: configuredApi.supplierMovePreview || "/api/suppliers/move-preview",
  // Header discount applied to a supplier's whole price list.
  supplierDiscount: configuredApi.supplierDiscount || "/api/suppliers/discount",
  // Past compilations, one dated folder per run.
  ordini: configuredApi.ordini || "/api/ordini",
  ordiniDelete: configuredApi.ordiniDelete || "/api/ordini/elimina",
  // AI-stage settings. The API key only ever travels in a POST body: the
  // local service logs the request line on every call, and a key passed as
  // a URL parameter would end up in that log.
  impostazioni: configuredApi.impostazioni || "/api/impostazioni",
  impostazioniModelli: configuredApi.impostazioniModelli || "/api/impostazioni/modelli",
  impostazioniChiave: configuredApi.impostazioniChiave || "/api/impostazioni/chiave",
  impostazioniProva: configuredApi.impostazioniProva || "/api/impostazioni/prova",
};

const mode = new URLSearchParams(window.location.search).get("demo") === "1" ? "demo" : "live";

// Closed state of the "move everything to another supplier" panel: an empty
// `from` means no move is in progress and no destination has been chosen.
function emptySupplierMove() {
  return {
    from: "",
    fromName: "",
    fromTotal: 0,
    loading: false,
    error: "",
    preview: null,
    choiceId: "",
  };
}

const state = {
  review: null,
  currentStep: 1,
  filters: {
    search: "",
    type: "all",
    status: "all",
    page: 1,
    pageSize: 20,
    // Set by "Show products" in the promotions panel (step 2): filters the
    // list to the products covered by that promotion. Resets as soon as the
    // user touches search/type/status again, or via the chip's "Remove
    // filter" button.
    promotionId: null,
    // Search over promotion condition text: with hundreds of entries,
    // scrolling the list isn't a way to find anything.
    promotionQuery: "",
    // Sort order for the step-2 product list. "gestionale" is the order the
    // management-software export lists products in — kept as the default,
    // since it's the order the person placing the order wrote them in — the
    // other two sort by name, the only way to find a product by name once
    // there are hundreds of rows.
    sort: "gestionale",
  },
  summary: {
    grouping: "supplier",
    supplierId: "all",
    sort: "name",
  },
  // The promotions panel holds the search field: if this isn't remembered,
  // the panel closes on every redraw and the field disappears while typing.
  promotionCatalogOpen: false,
  pendingFiles: [],
  // Dropped documents the app didn't recognize. Kept OUTSIDE `pendingFiles`,
  // the array the upload request is built from, so nothing here can be sent
  // by mistake. They stay until dismissed, since a toast that lives 3.6s is
  // not a place to keep information.
  fileScartati: [],
  // Consecutive failed save attempts: after three, the app stops retrying on
  // its own and leaves the retry action at the top of the page.
  tentativiDiSalvataggio: 0,
  excludedProductIds: new Set(),
  exclusionUndo: null,
  // productId -> { quantity, selectedSupplierId, confirmed }: what the
  // product had before being excluded.
  // Needed because `exclusionUndo` is a single slot that expires: the "…
  // was excluded. [Undo]" toast restored quantity and supplier, but "Put
  // back in the order" — the only action left after the toast expires, and
  // the only one after a reload — restored the product with its quantity
  // reset to zero. The product came back looking fine but wasn't ordered,
  // and the previous quantity was lost. Lives alongside the exclusion set
  // in browser storage, keyed by run id: cleared together with it on
  // recompute, which is correct — those choices were about the previous
  // comparison.
  sceltePrimaDellEsclusione: new Map(),
  // Counterpart to `exclusionUndo` for the "x" on the summary (step 3). Two
  // different actions — one excludes the product from the list, the other
  // zeroes its quantity — and both make the product disappear the instant
  // they're pressed. Holds both quantity AND confirmation: the action clears
  // both, since restoring only the quantity would bring the product back
  // without its confirmation, blocking compilation.
  rimozioneUndo: null,
  // Quantities zeroed in bulk by "Clear quantities proposed by the
  // management software", kept so they can be restored. The step-2 bulk
  // action: without undo it was the most destructive action on the screen
  // with the thinnest safety net — excluding a single product, which is
  // less destructive, already had one.
  azzeramentoUndo: null,
  // productId -> { supplierName, quantityText, previousFactor, newFactor, previousPieces, newPieces }
  // Temporarily highlights a card when a supplier change alters pieces per carton.
  supplierChangeNotices: new Map(),
  supplierChangeTimers: new Map(),
  // Moving all of a supplier's products to another supplier (step 3). Prices
  // and deltas come from the local service; only the source supplier and the
  // user's chosen destination are kept here.
  supplierMove: emptySupplierMove(),
  // Assignments and confirmations from before the last move: back "Restore
  // previous suppliers", same role as state.exclusionUndo for excluded
  // products.
  supplierMoveUndo: null,
  // "Start new comparison": `chiedendo` is the open confirmation prompt,
  // `inCorso` the window between confirming and the service's response.
  // Nothing else needs tracking: the outcome is read back from the rebuilt
  // page, not from a flag.
  nuovaComparazione: { chiedendo: false, inCorso: false },
  // Orders compiled last week and not yet confirmed as received. A nudge to
  // avoid reordering stock that's already on its way; if the local service
  // doesn't respond, the list stays empty and the page works as before.
  history: {
    pending: [],
    loading: false,
    // Why the pending-orders list is missing. Empty string = nothing pending;
    // non-empty = it couldn't be determined, a different situation entirely.
    errore: "",
    // orderId of the question currently being answered.
    answering: "",
    // orderId the user already answered in this session: not asked again
    // while the page stays open.
    answered: new Set(),
  },
  // Past compilations, read from GET /api/ordini by scanning the dated
  // folders. Not required for step 3: if the list fails to load, the panel
  // says so and the summary stays usable as before. The browser doesn't
  // recompute anything from this data, only displays it.
  compilazioni: {
    caricate: false,
    inCorso: false,
    elenco: [],
    errore: "",
    aperta: false,
    confermaElimina: "",
    eliminando: "",
  },
  uploadDeletion: { confirming: "", deleting: "" },
  matches: { answering: "" },
  catalog: {
    open: false,
    query: "",
    loading: false,
    results: [],
    error: "",
    timer: null,
  },
  // The price-list viewer: opens from a product and shows a supplier's price
  // list as the app parsed it. `prodottoId` is the product it was opened
  // from — the one being matched — and `rigaFuoco` the already-matched row
  // the viewer opens on.
  listino: {
    aperto: false,
    prodottoId: "",
    fornitore: "",
    query: "",
    // Sequence number of the last requested fetch: a response that doesn't
    // carry this number is stale and is discarded.
    richiesta: 0,
    da: 0,
    quante: 50,
    righe: [],
    fornitori: [],
    totale: 0,
    trovate: 0,
    scartate: 0,
    rigaFuoco: null,
    caricando: false,
    nome: "",
    errore: "",
    // The requested price list isn't there, and the service says why. Not
    // `errore`: the viewer itself works, it's that one supplier that failed
    // to load.
    problema: "",
    esito: "",
    abbinando: "",
    timer: null,
  },
  // Recompute of the comparison. `stato` is exactly what GET /api/pipeline/stato
  // returns: nothing is rebuilt here, just displayed. `chiesto` distinguishes
  // "I just pressed the button" from "the service remembers a previous run",
  // which must not make the progress bar appear. `controlliPersi` counts
  // consecutive failed status polls; `contattoPerso` is the message shown once
  // there are too many, since a stalled bar with no message reads as "still
  // working". `avviando`: the start request is in flight. Kept outside `stato`,
  // which is what the service sends, since only this tab knows it.
  pipeline: { stato: null, chiesto: false, errore: "", timer: null, controlliPersi: 0, contattoPerso: "", avviando: false },
  // Open/closed state of each collapsible section, by key: the page redraws
  // itself, and without this map sections would collapse under the user's
  // fingers. Value is `true` or `false` — key presence alone isn't enough,
  // since panels that start open need "closed" to be written explicitly,
  // not inferred from absence.
  aperti: {},
  schemaMapping: {
    loading: false,
    loadedRunId: "",
    data: null,
    values: {},
    results: {},
    error: "",
    validating: false,
    confirming: false,
    // The document whose columns are being reviewed manually, opened from
    // its card on step 1. Empty when the selector appears because the
    // pipeline stalled instead: that's a different path, restarted
    // automatically after confirmation.
    documento: "",
  },
  // Columns actually used by the current comparison, by document name.
  // Loaded once per recompute: they only change when the run changes.
  colonneDocumenti: { caricate: false, caricando: false, runId: "", perNome: {}, motivo: "", errore: "" },
  // The "which column to write the order into" panel. Kept separate from
  // `schemaMapping`, which is the guided mapping shown only when a document
  // isn't recognized: this one is always available.
  colonnaOrdine: {
    aperta: false,
    fornitore: "",
    nome: "",
    fileName: "",
    foglio: "",
    caricando: false,
    salvando: false,
    errore: "",
    colonne: [],
    attuale: null,
    scelta: "",
  },
  // `data-focus-key` of the control that opened the topmost modal: focus
  // returns there on close. Empty string means "that control had no key,
  // fall back to #workspace"; `null` means "no modal to close", needed
  // because the close functions are called even when nothing is open.
  fuocoPrimaDellaFinestra: null,
  loading: true,
  uploading: false,
  compiling: false,
  saving: false,
  dirty: false,
  saveVersion: 0,
  savedVersion: 0,
  // Version of the state ON DISK this tab started from. Unrelated to
  // saveVersion/savedVersion, which count this tab's own edits: lets the
  // local service detect that another tab saved in the meantime, instead of
  // letting two tabs silently overwrite each other's work.
  stateVersion: 0,
  savePromise: null,
  runtimeError: "",
  uploadMessage: "",
  compileResult: null,
  // A failed compilation: user-facing message and technical detail kept
  // separate, so the former stays readable and the latter is available
  // without taking up space.
  compileFailure: null,
  acceptBelowThreshold: false,
  saveTimer: null,
  quantityRenderTimer: null,
  // Settings page. Not a step of the flow: `currentStep` ranges 1..3 both
  // here and on the local service, and adding a fourth value would get it
  // truncated on save. A separate page, reachable from step 1.
  impostazioni: {
    aperta: false,
    caricamento: false,
    caricate: false,
    errore: "",
    // Only presence, source, and a four-character tail: the local service
    // never sends the actual key value back.
    chiave: { presente: false, origine: "", coda: "" },
    percorsoChiave: "",
    valori: {},
    predefinite: {},
    // "These two codes are the same item" declarations. Live here since this
    // is where they're read back, and they don't depend on the comparison:
    // they're permanent and this page opens even without one.
    uguaglianze: {
      caricate: false,
      caricando: false,
      query: "",
      voci: [],
      totale: 0,
      motivo: "",
      errore: "",
      togliendo: "",
      timer: null,
    },
    // The freshly pasted key lives only here, only for the time between
    // pasting and sending. Never enters the page's HTML: the field receives
    // it as a DOM property, which doesn't show up in the page source.
    nuovaChiave: "",
    salvandoChiave: false,
    salvando: false,
    provando: false,
    prova: null,
    messaggio: "",
    modelli: { elenco: [], caricamento: false, caricati: false, errore: "", avviso: "" },
  },
};

const appElement = document.querySelector("#app");
const stepperElement = document.querySelector("#stepper");
const globalMessageElement = document.querySelector("#global-message");
const modeBadgeElement = document.querySelector("#mode-badge");
const saveStatusElement = document.querySelector("#save-status");
const toastRegionElement = document.querySelector("#toast-region");

// `useGrouping: "always"`: the Italian `Intl` default doesn't group
// thousands below five digits, so the same column could show "1264,20 €"
// next to "12.345,68 €" — two formats for the same kind of number, and the
// first reads at a glance as an order of magnitude smaller than it is.
const euros = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "EUR",
  minimumFractionDigits: 2,
  useGrouping: "always",
});

const integers = new Intl.NumberFormat("it-IT", {
  maximumFractionDigits: 0,
});

// Models are billed in dollars, not euros: OpenRouter's price list is in
// dollars, and converting here would mean inventing an exchange rate.
const dollars = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

// "3 agosto": day and month, for prompts about orders not yet received.
const dayAndMonth = new Intl.DateTimeFormat("it-IT", {
  day: "numeric",
  month: "long",
});

// "lunedì 11 agosto": date of the comparison still being viewed. The weekday
// isn't decoration — it's how a person actually recalls when they uploaded
// the price lists — and it also keeps the surrounding Italian sentence
// grammatical ("di lunedì 11 agosto" reads correctly; "del 11 agosto" doesn't).
const weekdayDayMonth = new Intl.DateTimeFormat("it-IT", {
  weekday: "long",
  day: "numeric",
  month: "long",
});

// "19 agosto 2026": date of the running version. Unlike the other date
// formats, the year matters here: an install that's months out of date and
// one that's a year out of date need to be distinguishable at a glance,
// which is the whole reason this format exists.
const dayMonthYear = new Intl.DateTimeFormat("it-IT", {
  day: "numeric",
  month: "long",
  year: "numeric",
});

function demoReview() {
  return {
    run: {
      id: "DEMO-2026-08-10",
      status: "ready",
      createdAt: "2026-08-10T10:35:00+02:00",
      label: "Confronto agosto 2026",
    },
    files: [
      {
        name: "prova2.xlsx",
        kind: "Gestionale",
        supplier: "Gestionale",
        status: "ready",
        schemaState: "SCHEMA_NOTO",
        rows: 527,
        message: "527 prodotti letti; EAN utilizzato come riferimento.",
      },
      {
        name: "LISTINO BETULLA VALIDO FINO AL 01-09-26.xlsx",
        kind: "Listino",
        supplier: "BETULLA",
        status: "ready",
        schemaState: "SCHEMA_NOTO",
        rows: 6379,
        message: "Schema riconosciuto e controllo campione superato.",
      },
      {
        name: "32.1 03-06 ago.xlsx",
        kind: "Listino",
        supplier: "Larice",
        status: "warning",
        schemaState: "SCHEMA_VARIATO",
        rows: 6310,
        message: "Tre sconti testuali non standard sono stati trattati senza sconto.",
      },
      {
        name: "formattato_104233.xls",
        kind: "Listino",
        supplier: "Noce",
        status: "ready",
        schemaState: "SCHEMA_NOTO",
        rows: 8851,
        message: "Righe FOOD scartate in lettura; disponibilità e unità d'ordine controllate.",
      },
    ],
    suppliers: [
      { id: "betulla", name: "BETULLA", minimumOrder: 1000 },
      { id: "larice", name: "Larice", minimumOrder: 1000 },
      { id: "noce", name: "Noce", minimumOrder: 0 },
    ],
    warnings: [
      {
        id: "larice-discounts",
        severity: "warning",
        blocking: false,
        title: "Tre sconti Larice da tenere presenti",
        message: "I valori testuali ** sono stati interpretati senza sconto e restano registrati nei controlli.",
      },
    ],
    products: [
      {
        id: "p-solbao-aloe",
        name: "Solbao Profumo d'Aloe viso SPF 50+",
        ean: "8009822068627",
        itemType: "product",
        quantity: 8,
        suggestedQuantity: 8,
        quantitySource: "gestionale",
        lastUnitPrice: 5.95,
        orderUnitLabel: "colli",
        selectedSupplierId: "betulla",
        confirmed: true,
        requiresConfirmation: false,
        offers: [
          { supplierId: "betulla", price: 34.50, unitsPerOrderUnit: 6, available: true, matchStatus: "EAN esatto", lastPriceDifference: -0.20, lastPriceDifferencePct: -3.4 },
          { supplierId: "larice", price: 35.94, unitsPerOrderUnit: 6, available: true, matchStatus: "EAN esatto", lastPriceDifference: 0.04, lastPriceDifferencePct: 0.7 },
          { supplierId: "noce", price: 37.26, unitsPerOrderUnit: 6, available: true, matchStatus: "EAN esatto", lastPriceDifference: 0.26, lastPriceDifferencePct: 4.4 },
        ],
      },
      {
        id: "p-solbao-display-96",
        name: "Solbao espositore Top Performer x 96",
        ean: "",
        itemType: "display",
        quantity: 1,
        orderUnitLabel: "espositori",
        selectedSupplierId: "noce",
        confirmed: false,
        requiresConfirmation: true,
        confirmationMessage: "Noce propone 96 pezzi totali, ma due referenze differiscono dalla composizione BETULLA. Conferma che l'espositore sia equivalente per il tuo ordine.",
        warnings: [
          {
            id: "display-composition",
            severity: "warning",
            blocking: false,
            title: "Composizione non identica",
            message: "L'offerta selezionata è comparabile, non identica.",
          },
        ],
        components: [
          { name: "Solbao Fresco Estate Spray SPF 20", quantity: 6, ean: "8009060592120" },
          { name: "Solbao Fresco Estate Spray SPF 30", quantity: 6, ean: "8009037574043" },
          { name: "Solbao Fresco Estate Spray SPF 50+", quantity: 6, ean: "8009623230506" },
          { name: "Solbao Profumo d'Aloe Stick SPF 50+", quantity: 6, ean: "8009187593178" },
          { name: "Solbao Profumo d'Aloe viso SPF 50+", quantity: 6, ean: "8009822068627" },
          { name: "Solbao Pro Vitamina Stick SPF 50+", quantity: 6, ean: "8009311143422" },
          { name: "Solbao Pro Vitamina viso SPF 50+", quantity: 6, ean: "8009334159301" },
          { name: "Solbao Pro Vitamina latte 50+", quantity: 6, ean: "8009431397910" },
          { name: "Solbao Profumo d'Aloe latte SPF 30", quantity: 6, ean: "8009655638110" },
          { name: "Solbao Profumo d'Aloe Trigger 30", quantity: 6, ean: "8009752681972" },
          { name: "Solbao Pro Vitamina Trigger SPF 20", quantity: 6, ean: "8009503251027" },
          { name: "Solbao Pro Vitamina Trigger SPF 30", quantity: 6, ean: "8009222342549" },
          { name: "Solbao Profumo d'Aloe latte SPF 50+", quantity: 6, ean: "8009154530151" },
          { name: "Solbao Profumo d'Aloe Trigger SPF 50+", quantity: 6, ean: "8009151499314" },
          { name: "Solbao Pro Vitamina latte 30", quantity: 6, ean: "8009107082232" },
          { name: "Solbao Pro Vitamina Trigger SPF 50+", quantity: 6, ean: "8009451532339" },
        ],
        offers: [
          { supplierId: "betulla", price: 659.88, unitsPerOrderUnit: 96, available: true, matchStatus: "Espositore identico", compositionStatus: "identical" },
          { supplierId: "larice", price: 671.04, unitsPerOrderUnit: 96, available: true, matchStatus: "Espositore identico", compositionStatus: "identical" },
          { supplierId: "noce", price: 648.00, unitsPerOrderUnit: 96, available: true, matchStatus: "Composizione da confermare", compositionStatus: "comparable" },
        ],
      },
      {
        id: "p-neval-spray",
        name: "Neval Sun Protect & Moisture Spray SPF 50",
        ean: "4009111150600",
        itemType: "product",
        quantity: 6,
        suggestedQuantity: 4,
        quantitySource: "utente",
        orderUnitLabel: "colli",
        selectedSupplierId: "larice",
        confirmed: true,
        requiresConfirmation: false,
        offers: [
          { supplierId: "betulla", price: 58.80, unitsPerOrderUnit: 6, available: true, matchStatus: "EAN esatto" },
          { supplierId: "larice", price: 56.70, unitsPerOrderUnit: 6, available: true, matchStatus: "EAN esatto" },
          { supplierId: "noce", price: 60.18, unitsPerOrderUnit: 6, available: false, matchStatus: "Non disponibile" },
        ],
      },
      {
        id: "p-labrino",
        name: "Labrino Hydro Care 4,8 g",
        ean: "4009505492743",
        itemType: "product",
        quantity: 0,
        orderUnitLabel: "colli",
        selectedSupplierId: "",
        confirmed: false,
        requiresConfirmation: false,
        offers: [
          { supplierId: "betulla", price: 21.60, unitsPerOrderUnit: 12, available: true, matchStatus: "EAN esatto" },
          { supplierId: "larice", price: 20.88, unitsPerOrderUnit: 12, available: true, matchStatus: "EAN esatto" },
        ],
      },
      {
        id: "p-ambre-missing",
        name: "Soleil Doux Latte Kids SPF 50+ 200 ml",
        ean: "3609347487498",
        itemType: "product",
        quantity: 2,
        orderUnitLabel: "colli",
        selectedSupplierId: "",
        confirmed: false,
        requiresConfirmation: false,
        warnings: [
          {
            id: "missing-offer",
            severity: "error",
            blocking: true,
            title: "Nessun fornitore ce l’ha",
            message: "Nessuno dei listini caricati ha una riga utilizzabile per questo prodotto.",
          },
        ],
        offers: [],
      },
      {
        id: "p-solbao-core-66",
        name: "Solbao espositore Core x 66",
        ean: "",
        itemType: "display",
        quantity: 0,
        orderUnitLabel: "espositori",
        selectedSupplierId: "",
        confirmed: false,
        requiresConfirmation: false,
        components: [
          { name: "Solbao Latte SPF 30", quantity: 18, ean: "" },
          { name: "Solbao Spray SPF 30", quantity: 24, ean: "" },
          { name: "Solbao Stick SPF 50+", quantity: 24, ean: "" },
        ],
        offers: [
          { supplierId: "betulla", price: 419.76, unitsPerOrderUnit: 66, available: true, matchStatus: "Espositore identico", compositionStatus: "identical" },
          { supplierId: "larice", price: 425.70, unitsPerOrderUnit: 66, available: true, matchStatus: "Espositore identico", compositionStatus: "identical" },
        ],
      },
    ],
  };
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function finiteNumber(value, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

// Unlike finiteNumber(), a missing value (null/undefined) stays missing.
// Used for optional backend-computed fields (e.g. lastUnitPrice,
// suggestedQuantity) where "not available" and "zero" mean different things.
function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function userFacingText(value) {
  return String(value ?? "")
    // The replaced word changes gender/number, so the apostrophized article
    // before it must be resolved together with it — without this, the page
    // would show "nell'controlli". Handles the one case the app actually
    // produces; add more here if new ones show up.
    .replace(/\b(nell|dell|all|sull|dall|l)['’]audit\b/gi, (_intero, articolo) =>
      ({ nell: "nei", dell: "dei", all: "ai", sull: "sui", dall: "dai", l: "i" })[articolo.toLowerCase()] + " controlli")
    .replace(/\baudit\b/gi, "controlli")
    .replace(/\bbackend\b/gi, "servizio locale")
    .replace(/\bupload\b/gi, "caricamento")
    .replace(/\bfile\b/gi, "documento");
}

function friendlyMatchStatus(value) {
  const text = String(value || "Offerta disponibile");
  const known = {
    EAN_ESATTO: "EAN esatto",
    EAN_AMBIGUO: "EAN da verificare",
    EAN_ASSENTE: "EAN non presente",
    DA_VERIFICARE: "Da verificare",
    CATALOGO_FORNITORE: "Presente nel catalogo",
    LISTINO_AGGIORNATO_DA_VERIFICARE: "Listino aggiornato: da verificare",
    SEMANTICO_PROPOSTO: "Prodotto proposto",
    SEMANTICO_CONFERMATO_UTENTE: "Confermato",
    RIFIUTATO_UTENTE: "Rifiutato da te",
  };
  return known[text.toUpperCase()] || userFacingText(text);
}

// The normal case gets no badge. "EAN esatto" and "Espositore identico" just
// mean the match went as expected, and across twenty products with four
// suppliers that was eighty green badges per screen, all saying the same
// thing. Green is also how the selected offer is highlighted in the same
// card, so the one thing worth spotting at a glance shared its color with
// three that didn't matter. Keeping green for a single meaning fixes that.
// The text itself stays, next to the supplier name in gray, since it still
// says HOW the row was matched — it just stops competing for attention.
const ABBINAMENTI_NORMALI = new Set(["EAN esatto", "Espositore identico", "Confermato"]);

function abbinamentoNormale(offer) {
  return ABBINAMENTI_NORMALI.has(String(offer?.matchStatus || ""))
    && String(offer?.compositionStatus || "") !== "comparable";
}

function normalizeIssue(issue, fallbackId) {
  if (typeof issue === "string") issue = { message: issue };
  const severity = String(issue?.severity || issue?.level || "warning").toLowerCase();
  return {
    id: String(issue?.id || fallbackId),
    severity: severity === "error" || severity === "danger" ? "error" : severity === "info" ? "info" : "warning",
    blocking: Boolean(issue?.blocking ?? severity === "error"),
    title: userFacingText(issue?.title || (severity === "error" ? "Controllo necessario" : "Avviso")),
    message: userFacingText(issue?.message || issue?.detail || "Verifica richiesta."),
    productId: issue?.productId == null ? "" : String(issue.productId),
    // Product and supplier name: the service sends them with every failed
    // check, and they're the only way to find the row among five hundred.
    productName: String(issue?.productName ?? issue?.product_name ?? ""),
    supplierName: String(issue?.supplierName ?? issue?.supplier_name ?? ""),
    code: String(issue?.code || ""),
    supplierId: String(issue?.supplierId ?? issue?.supplier_id ?? ""),
    candidateKey: String(issue?.candidateKey ?? issue?.candidate_key ?? ""),
    technicalMessage: userFacingText(issue?.technicalMessage ?? issue?.technical_message ?? ""),
    candidate: normalizeCandidate(issue?.candidate),
  };
}

function normalizeCandidate(candidate) {
  if (!candidate || typeof candidate !== "object") return null;
  // Floor at 0, not 1: "unknown" must stay 0, not silently become "1 piece
  // per carton" printed as if it came from the price list. A zero factor is
  // exactly why the local service refuses to accept this candidate — without
  // pieces per carton, the order quantity can't be computed.
  const quantityFactor = Math.max(0, finiteNumber(
    candidate.quantityFactor
      ?? candidate.quantity_factor
      ?? candidate.unitsPerOrderUnit
      ?? candidate.units_per_order_unit,
    0,
  ));
  const orderUnitPriceNet = Math.max(0, finiteNumber(
    candidate.orderUnitPriceNet
      ?? candidate.order_unit_price_net
      ?? candidate.price,
    0,
  ));
  return {
    supplierId: String(candidate.supplierId ?? candidate.supplier_id ?? ""),
    supplierName: String(candidate.supplierName ?? candidate.supplier_name ?? ""),
    candidateKey: String(candidate.candidateKey ?? candidate.candidate_key ?? ""),
    description: String(candidate.description ?? candidate.name ?? ""),
    ean: String(candidate.ean ?? candidate.barcode ?? ""),
    supplierCode: String(candidate.supplierCode ?? candidate.supplier_code ?? ""),
    sourceRow: candidate.sourceRow ?? candidate.source_row ?? null,
    quantityFactor,
    orderUnitPriceNet,
    unitPriceNet: Math.max(0, finiteNumber(
      candidate.unitPriceNet ?? candidate.unit_price_net,
      quantityFactor > 0 ? orderUnitPriceNet / quantityFactor : 0,
    )),
    details: String(candidate.details ?? candidate.packaging ?? ""),
    // Why the automatic matcher rejected this row, e.g. "it's a 2-piece pack
    // while the searched item is sold single". The one field that answers
    // "is this the same item?": the card shows it instead of a similarity
    // score, which means nothing to whoever is placing the order.
    rationale: String(candidate.rationale ?? ""),
    available: candidate.available === true,
  };
}

// Pending orders: matching to products is decided by the local service on
// the EAN (the "product:<row>" ids change on every management-software
// export). Data arrives already matched here and is only displayed.
function normalizePendingOrder(entry) {
  return {
    orderId: String(entry?.orderId ?? entry?.order_id ?? ""),
    supplier: String(entry?.supplier ?? ""),
    supplierName: String(entry?.supplierName ?? entry?.supplier_name ?? entry?.supplier ?? "Fornitore"),
    createdAt: String(entry?.createdAt ?? entry?.created_at ?? entry?.orderedAt ?? entry?.ordered_at ?? ""),
    answeredAt: String(entry?.answeredAt ?? entry?.answered_at ?? ""),
    // When the question comes back after a "not received yet": the date is
    // decided by the local service, only compared against the clock here.
    askAgainAt: String(entry?.askAgainAt ?? entry?.ask_again_at ?? ""),
    lineCount: Math.max(0, Math.trunc(finiteNumber(entry?.lineCount ?? entry?.line_count, 0))),
    totalNet: Math.max(0, finiteNumber(entry?.totalNet ?? entry?.total_net, 0)),
  };
}

function normalizePendingOrders(list) {
  return asArray(list).map(normalizePendingOrder).filter((entry) => entry.orderId);
}

// Per-product entry from GET /api/review: quantity already ordered (in order
// units, i.e. cartons) and the order date.
function normalizeProductPendingOrder(entry) {
  return {
    orderId: String(entry?.orderId ?? entry?.order_id ?? ""),
    supplier: String(entry?.supplier ?? ""),
    supplierName: String(entry?.supplierName ?? entry?.supplier_name ?? entry?.supplier ?? "Fornitore"),
    orderedAt: String(entry?.orderedAt ?? entry?.ordered_at ?? entry?.createdAt ?? entry?.created_at ?? ""),
    quantity: Math.max(0, Math.trunc(finiteNumber(entry?.quantity, 0))),
    // Unit at order time, not necessarily this week's unit.
    unit: String(entry?.unit ?? ""),
    // How many products in this comparison share the same barcode: when more
    // than one, the ordered quantity can't be attributed to THIS item, and
    // the message must say so.
    sharedWith: Math.max(0, Math.trunc(finiteNumber(entry?.sharedWith ?? entry?.shared_with, 0))),
  };
}

const PROMOTION_KIND_LABELS = {
  sconto_numerico: "Sconto numerico",
  soglia_omaggio: "Soglia con omaggio",
  confezione_promozionale: "Confezione promozionale",
  offerta_ambigua: "Offerta da verificare",
};

function promotionKindLabel(kind) {
  return PROMOTION_KIND_LABELS[String(kind || "")] || "Condizione promozionale";
}

const PROMOTION_STATUS_BADGES = {
  ottenuta: { label: "Ottenuta", tone: "success" },
  vicina: { label: "Ci manca poco", tone: "warning" },
  non_raggiunta: { label: "Non raggiunta", tone: "neutral" },
  da_verificare: { label: "Da verificare", tone: "info" },
};

function promotionStatusBadge(status) {
  return PROMOTION_STATUS_BADGES[String(status || "")] || PROMOTION_STATUS_BADGES.da_verificare;
}

function findPromotion(promotionId) {
  return asArray(state.review?.promotions).find((promotion) => String(promotion?.id ?? "") === String(promotionId ?? "")) || null;
}

function promotionMatchedProductIds(promotion) {
  return asArray(promotion?.state?.matched_product_ids).map(String);
}

function promotionText(promotion) {
  if (!promotion) return "";
  if (typeof promotion === "string") return promotion;

  const kind = String(promotion.kind || "");
  const promotionState = promotion.state || {};
  const status = String(promotionState.status || "");
  const sourceText = String(promotion.source_text || promotion.sourceText || "").trim();
  const reward = promotion.reward || {};
  const effect = promotion.economic_effect || promotion.economicEffect || {};

  if (kind === "sconto_numerico") {
    const rate = finiteNumber(effect.discount_rate ?? effect.discountRate, 0) * 100;
    const percentage = rate > 0 ? `${rate.toLocaleString("it-IT", { maximumFractionDigits: 2 })}%` : "";
    return effect.already_applied
      ? `Sconto ${percentage || "numerico"} già compreso nel prezzo`
      : `Sconto ${percentage || "numerico"}${status === "ottenuta" ? " applicato" : " disponibile"}`;
  }

  if (kind === "soglia_omaggio") {
    const stateMessage = String(promotionState.message || "Promozione con omaggio disponibile.");
    const rewardDescription = String(reward.description || "").trim();
    // The threshold message already names the free item in almost every
    // case; appending it again would repeat it in the same sentence.
    if (!rewardDescription || stateMessage.includes(rewardDescription)) return stateMessage;
    return `${stateMessage} Omaggio: ${rewardDescription}`;
  }

  if (kind === "confezione_promozionale") {
    return String(reward.description || sourceText || "Contenuto aggiuntivo già incluso nella confezione.");
  }

  if (kind === "offerta_ambigua" || status === "da_verificare") {
    return sourceText
      ? `Condizioni da verificare: ${sourceText}`
      : "Condizioni della promozione da verificare.";
  }

  return String(promotionState.message || sourceText || promotion.label || promotion.message || "");
}

// What the app discarded while reading the price lists: unorderable rows,
// rows excluded by filters, and duplicate barcodes. The data already exists
// in `review_data.json` (`auditSummary`, written by the recompute pipeline).
// Nothing here is invented, only read back — a discard that isn't counted
// isn't a choice, it's lost data.
// Supplier names come from the payload being normalized, not from
// `state.review` (which at this point is still the *previous* review, or
// `null` on first load) — reading names from the old state would render raw
// supplier ids instead of their display names.
function normalizeDiscardedRows(payload, supplierNamesById) {
  const audit = payload && typeof payload === "object" ? payload : {};
  const sources = audit.sources && typeof audit.sources === "object" ? audit.sources : {};
  const nomeFornitore = (supplierId) =>
    (supplierNamesById && supplierNamesById.get(supplierId)) || supplierId;
  const contati = (value) => {
    if (!value || typeof value !== "object") return [];
    return Object.entries(value)
      .map(([label, count]) => ({ label: String(label), count: Math.max(0, Math.trunc(finiteNumber(count, 0))) }))
      .filter((voce) => voce.count > 0)
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label, "it"));
  };
  return asArray(audit.inputs)
    .map((voce) => {
      const supplierId = String(voce?.supplier_id ?? voce?.supplierId ?? "");
      const ruolo = String(voce?.role ?? "");
      const notOrderable = contati(voce?.rows_not_orderable ?? voce?.rowsNotOrderable);
      const lettura = voce?.reading && typeof voce.reading === "object" ? voce.reading : {};
      const excluded = contati(lettura.rows_excluded ?? lettura.rowsExcluded);
      const fonte = sources[supplierId] && typeof sources[supplierId] === "object" ? sources[supplierId] : {};
      const duplicateEans = Math.max(0, Math.trunc(finiteNumber(fonte.duplicate_ean_values ?? fonte.duplicateEanValues, 0)));
      return {
        supplierId,
        supplierName: supplierId ? nomeFornitore(supplierId) : ruolo === "master" ? "Gestionale" : "Documento",
        rowsKept: Math.max(0, Math.trunc(finiteNumber(lettura.rows_kept ?? voce?.records, 0))),
        notOrderable,
        excluded,
        notOrderableCount: notOrderable.reduce((sum, item) => sum + item.count, 0),
        excludedCount: excluded.reduce((sum, item) => sum + item.count, 0),
        duplicateEans,
      };
    })
    .filter((voce) => voce.notOrderableCount || voce.excludedCount || voce.duplicateEans);
}

function normalizeOrderSummary(payload) {
  const summary = payload && typeof payload === "object" ? payload : {};
  return {
    suppliers: asArray(summary.suppliers).map((entry) => ({
      supplierId: String(entry?.supplierId ?? ""),
      supplierName: String(entry?.supplierName ?? entry?.supplierId ?? ""),
      lineCount: Math.max(0, Math.trunc(finiteNumber(entry?.lineCount, 0))),
      totalNet: finiteNumber(entry?.totalNet, 0),
      linesTotalNet: finiteNumber(entry?.linesTotalNet, 0),
      roundingDifference: finiteNumber(entry?.roundingDifference, 0),
    })),
    totalNet: finiteNumber(summary.totalNet, 0),
    linesTotalNet: finiteNumber(summary.linesTotalNet, 0),
    roundingDifference: finiteNumber(summary.roundingDifference, 0),
  };
}

function normalizeReview(payload) {
  if (!payload || typeof payload !== "object") {
    throw new Error("Il servizio dati ha restituito una risposta non valida.");
  }

  const suppliers = asArray(payload.suppliers).map((supplier, index) => ({
    id: String(supplier.id ?? `supplier-${index + 1}`),
    name: String(supplier.name ?? supplier.label ?? `Fornitore ${index + 1}`),
    minimumOrder: Math.max(0, finiteNumber(supplier.minimumOrder ?? supplier.minimum_order, 0)),
  }));

  const supplierById = new Map(suppliers.map((supplier) => [supplier.id, supplier]));
  // Products the page had to reassign on its own because the saved supplier
  // no longer has an available offer. The reassignment itself is fine;
  // doing it silently isn't: a user who picked a supplier by hand should be
  // told when the page switches them to a different one.
  const supplierReassignments = [];
  const products = asArray(payload.products).map((product, productIndex) => {
    const id = String(product.id ?? product.productId ?? `product-${productIndex + 1}`);
    const itemType = String(product.itemType ?? product.item_type ?? "product").toLowerCase();
    const normalizedItemType = itemType === "display" || itemType === "expositor" || itemType === "espositore" ? "display" : itemType === "kit" ? "kit" : "product";
    const quantity = Math.max(0, finiteNumber(product.quantity ?? product.orderQuantity ?? product.order_quantity, 0));
    const offers = asArray(product.offers).map((offer, offerIndex) => {
      const supplierId = String(offer.supplierId ?? offer.supplier_id ?? "");
      // For displays, the quantity in the price list almost always refers to
      // one display, while the piece count inside it is a separate field.
      // Preferring that field avoids wrongly showing "1 piece each" and
      // computing a per-piece price equal to the whole display's price.
      const unitsPerOrderUnit = Math.max(1, finiteNumber(
        normalizedItemType === "display"
          ? (offer.declaredUnits
            ?? offer.declared_units
            ?? offer.unitsPerOrderUnit
            ?? offer.units_per_order_unit
            ?? offer.quantityFactor
            ?? offer.quantity_factor
            ?? offer.packSize)
          : (offer.quantityFactor
            ?? offer.quantity_factor
            ?? offer.unitsPerOrderUnit
            ?? offer.units_per_order_unit
            ?? offer.packSize),
        1,
      ));
      const price = Math.max(0, finiteNumber(
        offer.orderUnitPriceNet
        ?? offer.order_unit_price_net
        ?? offer.price
        ?? offer.netPrice
        ?? offer.net_price,
        0,
      ));
      const promotionItems = asArray(offer.promotions);
      const legacyPromotion = String(
        offer.promotion?.label
        ?? offer.promotion?.message
        ?? offer.promotion
        ?? offer.offerDetails
        ?? offer.offer_details
        ?? offer.incentive
        ?? "",
      );
      return {
        id: String(offer.id ?? `${id}-offer-${offerIndex + 1}`),
        supplierId,
        supplierName: String(offer.supplierName ?? supplierById.get(supplierId)?.name ?? supplierId ?? "Fornitore"),
        description: String(offer.description ?? offer.name ?? ""),
        ean: String(offer.ean ?? offer.barcode ?? ""),
        supplierCode: String(offer.supplierCode ?? offer.supplier_code ?? ""),
        sourceRow: offer.sourceRow ?? offer.source_row ?? null,
        method: String(offer.method ?? ""),
        rationale: String(offer.rationale ?? ""),
        details: String(offer.details ?? ""),
        candidateDecision: String(offer.candidateDecision ?? offer.candidate_decision ?? ""),
        rejectedCandidate: normalizeCandidate(offer.rejectedCandidate ?? offer.rejected_candidate),
        price,
        unitsPerOrderUnit,
        quantityFactor: unitsPerOrderUnit,
        orderUnitPriceNet: price,
        pricePerPiece: Math.max(0, finiteNumber(offer.pricePerPiece ?? offer.price_per_piece, price / unitsPerOrderUnit)),
        available: offer.available !== false,
        // The rejection already recorded for this row, if any: set by the
        // service from the confirmations store. Without carrying it here,
        // the page would say the supplier "doesn't have it in the current
        // price list", which is false — they do have the row, the user said
        // it's a different item — and there'd be no way back from that.
        rifiutata: offer.rifiutata && typeof offer.rifiutata === "object" ? {
          since: String(offer.rifiutata.since ?? ""),
          description: String(offer.rifiutata.description ?? ""),
        } : null,
        // Confirmation can be required by a single offer, not only by the
        // product: without carrying it here, switching a product to an offer
        // that requires it gets the save rejected by the local service with
        // no checkbox on the page to give that confirmation.
        requiresConfirmation: Boolean(offer.requiresConfirmation ?? offer.requires_confirmation),
        confirmationMessage: String(offer.confirmationMessage ?? offer.confirmation_message ?? ""),
        matchStatus: friendlyMatchStatus(offer.matchStatus ?? offer.match_status ?? "Offerta disponibile"),
        compositionStatus: String(offer.compositionStatus ?? offer.composition_status ?? ""),
        warning: String(offer.warning ?? ""),
        promotions: promotionItems,
        promotion: promotionItems.map(promotionText).filter(Boolean).join(" · ") || legacyPromotion,
        // Computed by the backend against the last paid price (product.lastUnitPrice);
        // the frontend only displays these, never recomputes them.
        lastPriceDifference: optionalNumber(offer.lastPriceDifference ?? offer.last_price_difference),
        lastPriceDifferencePct: optionalNumber(offer.lastPriceDifferencePct ?? offer.last_price_difference_pct),
      };
    });

    let selectedSupplierId = String(product.selectedSupplierId ?? product.selected_supplier_id ?? "");
    if (!offers.some((offer) => offer.supplierId === selectedSupplierId && offer.available)) {
      // The cheapest supplier is always chosen on price per piece, never on
      // total per carton: cartons of different sizes (e.g. 6 vs 24 pieces)
      // aren't comparable on the total.
      const precedente = selectedSupplierId;
      selectedSupplierId = offers
        .filter((offer) => offer.available)
        .sort((a, b) => a.pricePerPiece - b.pricePerPiece)[0]?.supplierId || "";
      // Only counts products that actually had a supplier and now have a
      // different one: a product that was never assigned wasn't "moved".
      if (precedente && selectedSupplierId && precedente !== selectedSupplierId) {
        supplierReassignments.push({
          productId: id,
          productName: String(product.name ?? product.description ?? id),
          fromSupplierId: precedente,
          fromSupplierName: String(supplierById.get(precedente)?.name || precedente),
          toSupplierId: selectedSupplierId,
          toSupplierName: String(
            offers.find((offer) => offer.supplierId === selectedSupplierId)?.supplierName
            || supplierById.get(selectedSupplierId)?.name
            || selectedSupplierId,
          ),
        });
      }
    }

    // suggestedQuantity/quantitySource come from the management-software export:
    // until the user edits the quantity, the card shows a small "from the
    // management software" indicator.
    const suggestedQuantityRaw = product.suggestedQuantity ?? product.suggested_quantity;
    const suggestedQuantity = suggestedQuantityRaw === null || suggestedQuantityRaw === undefined
      ? null
      : Math.max(0, Math.trunc(finiteNumber(suggestedQuantityRaw, 0)));
    const quantitySource = String(product.quantitySource ?? product.quantity_source ?? "utente").toLowerCase() === "gestionale"
      ? "gestionale"
      : "utente";

    return {
      id,
      name: String(product.name ?? product.description ?? `Prodotto ${productIndex + 1}`),
      ean: String(product.ean ?? product.barcode ?? ""),
      itemType: normalizedItemType,
      quantity,
      suggestedQuantity,
      quantitySource,
      // Last net price paid per piece (optional, computed by the backend).
      lastUnitPrice: optionalNumber(product.lastUnitPrice ?? product.last_unit_price),
      orderUnitLabel: String(product.orderUnitLabel ?? product.order_unit_label ?? (itemType === "display" ? "espositori" : "colli")),
      selectedSupplierId,
      confirmed: Boolean(product.confirmed),
      requiresConfirmation: Boolean(product.requiresConfirmation ?? product.requires_confirmation),
      // Always shown when confirmation is required, so it must say what to
      // check, not just ask for a checkbox with no basis given.
      confirmationMessage: String(product.confirmationMessage ?? product.confirmation_message ?? "Il codice a barre non coincide: confronta nome e formato con quello qui sotto."),
      // The confirmation already recorded in the store: who, since when, on
      // which item. Read-only — never sent back in `snapshot()`, which only
      // sends `confirmed` — and absent until an actual answer was given.
      confirmation: product.confirmation && typeof product.confirmation === "object" ? {
        supplierId: String(product.confirmation.supplierId ?? ""),
        since: String(product.confirmation.since ?? ""),
        article: String(product.confirmation.article ?? ""),
      } : null,
      components: asArray(product.components).map((component) => ({
        name: String(component.name ?? component.description ?? "Componente"),
        quantity: Math.max(0, finiteNumber(component.quantity, 0)),
        ean: String(component.ean ?? component.barcode ?? ""),
      })),
      warnings: asArray(product.warnings).map((issue, index) => normalizeIssue(
        typeof issue === "string" ? { message: issue, productId: id } : { ...issue, productId: id },
        `${id}-warning-${index + 1}`,
      )),
      offers,
      addedManually: Boolean(product.addedManually ?? product.added_manually),
      excluded: Boolean(product.excluded),
      promotions: asArray(product.promotions),
      // Read-only informational display only: never sent in snapshot() and
      // never changes quantity, prices or the selected supplier.
      pendingOrders: asArray(product.pendingOrders ?? product.pending_orders)
        .map(normalizeProductPendingOrder)
        .filter((entry) => entry.orderId),
    };
  });

  return {
    run: {
      id: String(payload.run?.id ?? payload.runId ?? ""),
      status: String(payload.run?.status ?? payload.status ?? "ready"),
      createdAt: String(payload.run?.createdAt ?? payload.run?.created_at ?? ""),
      label: String(payload.run?.label ?? "Confronto fornitori"),
      // Which recompute produced this comparison. Used to know when the
      // columns shown on document cards need to be re-fetched: they only
      // change when the run that used them changes.
      pipelineRunId: String(payload.run?.pipelineRunId ?? payload.run?.pipeline_run_id ?? ""),
    },
    files: asArray(payload.files ?? payload.sources).map((file, index) => ({
      id: String(file.id ?? `file-${index + 1}`),
      name: String(file.name ?? file.filename ?? `Documento ${index + 1}`),
      kind: userFacingText(file.kind ?? file.type ?? "Listino"),
      supplier: String(file.supplier ?? file.supplierName ?? "Da riconoscere"),
      // Must be copied through: `renderColonnaDellOrdine(file, ...)` only
      // draws the "change column" action when `file.supplierId` is set. The
      // service always sends this field; dropping it here silently hides
      // that action while the "order written to column C" line above it
      // still renders.
      supplierId: String(file.supplierId ?? file.supplier_id ?? ""),
      status: String(file.status ?? "ready").toLowerCase(),
      schemaState: String(file.schemaState ?? file.schema_state ?? ""),
      rows: Math.max(0, finiteNumber(file.rows ?? file.rowCount ?? file.row_count, 0)),
      message: userFacingText(file.message ?? ""),
      deletable: Boolean(file.deletable),
      uploadName: String(file.uploadName ?? file.upload_name ?? file.name ?? ""),
    })),
    suppliers,
    products,
    warnings: asArray(payload.warnings).map((issue, index) => normalizeIssue(issue, `review-warning-${index + 1}`)),
    promotions: asArray(payload.promotions),
    promotionSummary: payload.promotionSummary && typeof payload.promotionSummary === "object" ? payload.promotionSummary : { counts: {} },
    // Price-list read discards and the summary totals, read from fields the
    // local service already sends.
    discardedRows: normalizeDiscardedRows(
      payload.auditSummary ?? payload.audit_summary,
      new Map(suppliers.map((supplier) => [supplier.id, supplier.name])),
    ),
    orderSummary: normalizeOrderSummary(payload.orderSummary ?? payload.order_summary),
    // Header discounts the service already applied to prices, as a
    // percentage, only for the field that displays them. Must be read from
    // `payload.state`: `state.review.state` doesn't exist, so this field
    // needs to be copied out explicitly or it renders empty on every
    // refresh even though the prices are already discounted.
    supplierDiscounts: normalizeSupplierDiscounts(payload.state?.supplierDiscounts),
    // Barcode pairs declared to be the same item. Permanent, across all
    // suppliers: viewed and removed from the price-list viewer, where
    // they're created.
    uguaglianze: asArray(payload.uguaglianze).map((voce) => ({
      codici: asArray(voce?.codici).map((codice) => String(codice)),
      motivo: String(voce?.motivo ?? ""),
      dal: String(voce?.valida_dal ?? ""),
    })).filter((voce) => voce.codici.length === 2),
    supplierReassignments,
  };
}

// Percentages, not fractions: the service already sends them multiplied by
// 100, matching the field where the user types "6".
function normalizeSupplierDiscounts(value) {
  if (!value || typeof value !== "object") return {};
  const sconti = {};
  for (const [supplierId, percento] of Object.entries(value)) {
    const numero = finiteNumber(percento, 0);
    if (numero > 0) sconti[String(supplierId)] = numero;
  }
  return sconti;
}

// Groups reassignments by from→to supplier pair, since that pair is what the
// user recognizes, not the individual product.
function supplierReassignmentNotices() {
  const gruppi = new Map();
  for (const voce of asArray(state.review?.supplierReassignments)) {
    const chiave = `${voce.fromSupplierId}→${voce.toSupplierId}`;
    if (!gruppi.has(chiave)) {
      gruppi.set(chiave, { from: voce.fromSupplierName, to: voce.toSupplierName, products: [] });
    }
    gruppi.get(chiave).products.push(voce.productName);
  }
  return [...gruppi.values()];
}

function renderSupplierReassignments() {
  const gruppi = supplierReassignmentNotices();
  if (!gruppi.length) return "";
  return `
    <section class="panel reassignment-panel" role="status">
      <div class="panel__header">
        <div>
          <h3>Alcuni fornitori sono cambiati da soli</h3>
          <p>Il fornitore che avevi scelto non ha più questi prodotti nel suo listino, quindi è stato preso il più conveniente fra quelli rimasti. Puoi cambiarlo a mano qui sotto.</p>
        </div>
        ${renderBadge(`${formatInteger(gruppi.reduce((somma, gruppo) => somma + gruppo.products.length, 0))} cambiati`, "warning")}
      </div>
      <ul class="reassignment-list">
        ${gruppi.map((gruppo) => {
          const quanti = gruppo.products.length;
          const testa = quanti === 1
            ? `1 prodotto era assegnato a ${gruppo.from}: ora è passato a ${gruppo.to}.`
            : `${formatInteger(quanti)} prodotti erano assegnati a ${gruppo.from}: ora sono passati a ${gruppo.to}.`;
          return `
            <li>
              <strong>${escapeHtml(testa)}</strong>
              <details class="reassignment-list__which" ${apribile(`riassegnati:${gruppo.from}:${gruppo.to}`)}><summary>Quali</summary><p>${escapeHtml(gruppo.products.join(" · "))}</p></details>
            </li>`;
        }).join("")}
      </ul>
    </section>`;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatEuro(value) {
  return euros.format(finiteNumber(value));
}

function formatPercent(value) {
  return `${finiteNumber(value).toLocaleString("it-IT", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}%`;
}

// Picks the singular or plural Italian form based on count.
function contati(quanti, singolare, plurale) {
  return `${formatInteger(quanti)} ${Number(quanti) === 1 ? singolare : plurale}`;
}

function formatInteger(value) {
  return integers.format(finiteNumber(value));
}

function formatDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("it-IT", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(date);
}

function formatDayMonth(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return dayAndMonth.format(date);
}

function formatWeekdayDayMonth(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return weekdayDayMonth.format(date);
}

function formatDayMonthYear(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return dayMonthYear.format(date);
}

function formatBytes(bytes) {
  const value = finiteNumber(bytes);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toLocaleString("it-IT", { maximumFractionDigits: 1 })} KB`;
  return `${(value / (1024 * 1024)).toLocaleString("it-IT", { maximumFractionDigits: 1 })} MB`;
}

function supplierName(supplierId) {
  return state.review?.suppliers.find((supplier) => supplier.id === supplierId)?.name || supplierId || "Non scelto";
}

function isManagementFile(file) {
  const text = `${file.kind} ${file.supplier}`.toLocaleLowerCase("it");
  return text.includes("gestional") || text.includes("prodotti da ordinare") || text.includes("ordine interno");
}

function excludedStorageKey() {
  return `confronto-fornitori:esclusi:${state.review?.run.id || "corrente"}`;
}

function restoreExcludedProducts() {
  // Called right after every state.review replacement: any supplier-change
  // notices left from the previous comparison no longer apply.
  for (const timer of state.supplierChangeTimers.values()) window.clearTimeout(timer);
  state.supplierChangeNotices.clear();
  state.supplierChangeTimers.clear();
  // Same for a supplier move in progress: its preview was computed against
  // the previous products, and undoing it would restore assignments that no
  // longer apply.
  state.supplierMove = emptySupplierMove();
  state.supplierMoveUndo = null;
  // Same for exclusion undo: it must be cleared here too. Otherwise the "…
  // was excluded. [Undo]" toast from before a recompute would restore the
  // supplier and confirmation FROM THE PREVIOUS COMPARISON onto a product
  // in the new one, which can have different offers and prices by now.
  state.exclusionUndo = null;
  // Same reasoning for the summary's "x" undo and the bulk quantity reset:
  // the saved quantities belonged to the previous comparison.
  state.rimozioneUndo = null;
  state.azzeramentoUndo = null;
  const valid = new Set(state.review?.products.map((product) => product.id) || []);
  const fromServer = state.review?.products
    .filter((product) => product.excluded)
    .map((product) => product.id) || [];
  if (mode === "demo") {
    state.excludedProductIds = new Set(fromServer.filter((id) => valid.has(id)));
    state.sceltePrimaDellEsclusione = new Map();
    return;
  }
  try {
    const stored = JSON.parse(localStorage.getItem(excludedStorageKey()) || "[]");
    // Two storage shapes, and the old one must still be readable: earlier
    // versions stored only a plain id list, and browsers upgraded from that
    // still have it. A list with no saved choices isn't an error — it just
    // means the previous quantity for those products isn't known, the same
    // as the older behavior.
    const elenco = Array.isArray(stored) ? stored : asArray(stored?.esclusi);
    const scelte = (!Array.isArray(stored) && stored && typeof stored.scelte === "object") ? stored.scelte : {};
    state.excludedProductIds = new Set(
      [...fromServer, ...elenco.map(String)].filter((id) => valid.has(id)),
    );
    state.sceltePrimaDellEsclusione = new Map(
      Object.entries(scelte)
        .filter(([id]) => valid.has(String(id)) && state.excludedProductIds.has(String(id)))
        .map(([id, scelta]) => [String(id), {
          quantity: Math.max(0, Math.trunc(finiteNumber(scelta?.quantity))),
          selectedSupplierId: String(scelta?.selectedSupplierId || ""),
          confirmed: Boolean(scelta?.confirmed),
        }]),
    );
  } catch {
    state.excludedProductIds = new Set(fromServer.filter((id) => valid.has(id)));
    state.sceltePrimaDellEsclusione = new Map();
  }
}

function persistExcludedProducts() {
  if (mode === "demo") return;
  try {
    localStorage.setItem(excludedStorageKey(), JSON.stringify({
      esclusi: [...state.excludedProductIds],
      scelte: Object.fromEntries(
        [...state.sceltePrimaDellEsclusione].filter(([id]) => state.excludedProductIds.has(id)),
      ),
    }));
  } catch {
    // Browser storage being unavailable must not block placing the order.
  }
}

function isExcluded(product) {
  return state.excludedProductIds.has(product.id);
}

// Collapsible sections must remember their own open/closed state in
// `state.aperti`, keyed by name, because `render()` replaces the whole DOM
// (`appElement.innerHTML = …`) instead of patching it — a <details> element's
// open state lives on the node, and the node is discarded and rebuilt closed
// on every redraw. This matters most during a recompute, where the page
// redraws on every poll tick (RITMO_PIPELINE_MS).
//
// One map, one key per panel: separate flags per panel would be easy to
// miss one of. Keys that depend on a specific product include its id, so two
// different cards don't open and close together.
//
// `apertoDiDefault` covers panels that start OPEN — e.g. a supplier's
// product list in the summary, which the user can collapse. For those, the
// map must also remember an explicit "closed" (`false`), not just delete the
// key on close: a deleted key falls back to the default (open) on the next
// redraw, which would silently reopen the panel the user just closed.
function apribile(chiave, apertoDiDefault = false) {
  const nome = String(chiave);
  const ricordato = state.aperti[nome];
  const aperto = ricordato === undefined ? apertoDiDefault : ricordato;
  return `data-ricorda="${escapeHtml(nome)}"${aperto ? " open" : ""}`;
}

function ricordaApertura(target) {
  const chiave = target && target.dataset ? target.dataset.ricorda : "";
  if (!chiave) return;
  state.aperti[chiave] = Boolean(target.open);
}

function selectedOffer(product) {
  return product.offers.find((offer) => offer.supplierId === product.selectedSupplierId && offer.available) || null;
}

// No supplier can fulfill this product: not "the user hasn't chosen yet",
// but "there's nothing to choose from". Mirrors the same check the local
// service makes in `nessuna_offerta_utilizzabile` (server.py): a quantity
// with no supplier is a valid state, and ends up in the "to be sourced"
// list at compile time.
function nessunaOffertaUtilizzabile(product) {
  return !asArray(product?.offers).some((offer) => offer.available);
}

// How many suppliers are excluded for this product because of a previous
// rejection, not because they don't carry it. Counts suppliers, not rows: a
// price list listing the same item twice must not count twice.
function rifiutatiDaTe(product) {
  return new Set(
    asArray(product?.offers)
      .filter((offer) => !offer.available && offer.rifiutata)
      .map((offer) => offer.supplierId),
  ).size;
}

// The two open questions about matching, which are really the same task —
// confirming an item is the right one: "is this the same product?" on a
// row a supplier proposes, and the confirmation checkbox on the selected
// offer. Kept apart from the general "to verify" filter, which also covers
// unrelated warnings like products no supplier carries: merging them back
// would put two different lists under one name.
function daConfermare(product) {
  if (confirmationRequired(product) && !product.confirmed) return true;
  return asArray(product?.offers).some(
    (offer) => offer.rejectedCandidate?.candidateKey && !offer.candidateDecision,
  );
}

// The user enters CARTONS, not pieces: no rounding, no leftover. Applies to
// every item type (product, display, kit) and every supplier.
function offerCalculation(product, offer, desiredQuantity = orderQuantity(product)) {
  const desired = Math.max(0, Math.trunc(finiteNumber(desiredQuantity)));
  const factor = Math.max(1, finiteNumber(offer?.quantityFactor ?? offer?.unitsPerOrderUnit, 1));
  const unitPrice = Math.max(0, finiteNumber(offer?.orderUnitPriceNet ?? offer?.price, 0));

  return {
    desired,
    factor,
    orderUnits: desired,
    orderedPieces: desired * factor,
    unitPrice,
    total: desired * unitPrice,
    // Uses the price list's own per-piece price when available, not
    // carton price / pieces: that division can round differently than the
    // supplier's stated per-piece price, producing a value that disagrees
    // with the one the "price per piece" column elsewhere shows. Using the
    // supplier's number keeps it consistent everywhere it's shown.
    pricePerPiece: finiteNumber(offer?.pricePerPiece) > 0
      ? finiteNumber(offer.pricePerPiece)
      : (factor > 0 ? unitPrice / factor : 0),
  };
}

// Per-piece price in the step-3 summary, next to the line total, in gray.
// Sits under a "price per piece" column header when there's room; on narrow
// screens that header collapses (see `.offer-grid__head`) and the inline
// label next to the number becomes the only cue. The column header is
// `aria-hidden` while this inline label always renders, so a screen reader
// announces "1,67 € al pezzo" either way, not an extra unlabeled row.
function renderPrezzoAlPezzo(calculation) {
  if (!calculation) return '<span class="order-lines__pezzo"></span>';
  return `<span class="order-lines__pezzo">${formatEuro(calculation.pricePerPiece)}<span class="order-lines__pezzo-etichetta"> al pezzo</span></span>`;
}

// The cheapest supplier is ALWAYS chosen on price per piece, never on total
// per carton: cartons with different piece counts aren't comparable on total.
// Compares an offer's per-piece price with the last price paid for that
// product. Uses lastPriceDifference/lastPriceDifferencePct from the backend
// when available, falling back to an equivalent client-side comparison.
function lastPriceComparison(product, offer) {
  if (offer.lastPriceDifference != null) {
    return {
      amount: offer.lastPriceDifference,
      percent: offer.lastPriceDifferencePct != null ? offer.lastPriceDifferencePct : 0,
    };
  }
  if (product.lastUnitPrice == null) return null;
  const amount = offer.pricePerPiece - product.lastUnitPrice;
  const percent = product.lastUnitPrice > 0 ? (amount / product.lastUnitPrice) * 100 : 0;
  return { amount, percent };
}

function orderQuantity(product) {
  return Math.max(0, finiteNumber(product.quantity));
}

function orderedProducts() {
  return state.review.products.filter((product) => !isExcluded(product) && orderQuantity(product) > 0);
}

function snapshot() {
  return {
    runId: state.review?.run.id || "",
    // The version this tab started from: if disk has a newer one, the save
    // is rejected instead of silently overwriting what another tab wrote.
    stateVersion: state.stateVersion,
    currentStep: state.currentStep,
    acceptBelowThreshold: Boolean(state.acceptBelowThreshold),
    summaryGrouping: state.summary.grouping,
    products: state.review?.products.map((product) => ({
      id: product.id,
      quantity: orderQuantity(product),
      selectedSupplierId: product.selectedSupplierId || null,
      confirmed: Boolean(product.confirmed),
      excluded: isExcluded(product),
      // Optional: lets the local service remember the user already edited
      // the quantity, so the "from the management software" indicator
      // doesn't reappear on reload.
      quantitySource: product.quantitySource,
    })) || [],
  };
}

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      cache: "no-store",
      ...options,
      headers: {
        Accept: "application/json",
        ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}),
      },
    });
  } catch (error) {
    throw new Error(`Impossibile raggiungere ${url}. Verifica che l'app locale sia avviata. (${error.message})`);
  }

  const contentType = response.headers.get("content-type") || "";
  const attesoJson = contentType.includes("application/json");
  let body;
  // A response that claims to be JSON but fails to parse is not an empty
  // response. Treating it as `{}` would make /api/review look like a
  // comparison with "0 products" — indistinguishable from a genuinely empty
  // one — instead of surfacing a readable error.
  let illeggibile = false;
  if (attesoJson) {
    try {
      body = await response.json();
    } catch {
      body = {};
      illeggibile = true;
    }
  } else {
    body = await response.text().catch(() => "");
  }

  if (!response.ok) {
    const detail = typeof body === "object" && body ? body.message || body.error : body;
    const errore = new Error(detail || `${response.status} ${response.statusText}`);
    // The response body travels with the error: it lists every failed check
    // individually, sent by the service on purpose. Dropping it here would
    // leave only a truncated summary with no way to find the actual rows.
    errore.dettagli = typeof body === "object" && body ? body : null;
    throw errore;
  }
  if (illeggibile) {
    throw new Error(`Il servizio locale ha risposto in un modo che non si riesce a leggere (${url}).`);
  }

  if (!contentType.includes("application/json")) {
    throw new Error("Il servizio locale ha restituito una risposta non valida.");
  }

  // Applied here, centrally, not in individual handlers. Every response
  // carrying a new state version updates this tab's copy: if a version bump
  // isn't collected somewhere, this tab's own next save gets rejected as
  // stale by a version it never even saw. Listing call sites by hand was
  // fragile — match, reject and discount all advance the version and any one
  // of them could be missed. Centralizing here means a new handler can't
  // forget it.
  //
  // Applied after the `response.ok` check, not before: an error response
  // carries the version of whoever actually wrote successfully, and applying
  // it here would let the next save attempt pass the staleness check and
  // overwrite what that other write just saved. A rejection must stay a
  // rejection.
  applyStateVersion(body);

  return body;
}

async function loadReview() {
  state.loading = true;
  state.runtimeError = "";
  render();
  try {
    const payload = mode === "demo" ? demoReview() : await requestJson(API.review);
    state.review = normalizeReview(payload);
    state.currentStep = Math.max(1, Math.min(3, Math.trunc(finiteNumber(payload.currentStep ?? payload.state?.currentStep, 1))));
    state.acceptBelowThreshold = Boolean(payload.acceptBelowThreshold ?? payload.state?.acceptBelowThreshold);
    state.summary.grouping = payload.summaryGrouping ?? payload.state?.summaryGrouping ?? "supplier";
    if (!['supplier', 'product'].includes(state.summary.grouping)) state.summary.grouping = 'supplier';
    state.stateVersion = Math.max(0, Math.trunc(finiteNumber(payload.state?.stateVersion ?? payload.stateVersion, 0)));
    restoreExcludedProducts();
    state.loading = false;
    state.uploadMessage = "";
    updateSaveStatus(mode === "demo" ? "Dati dimostrativi, nessun salvataggio" : "Tutto salvato");
    render();
    if (state.currentStep <= 2) loadPendingOrders();
    // A page reload landing directly on the summary must show the
    // compilation history without leaving and re-entering that step.
    if (state.currentStep === 3) loadCompilazioni();
    caricaColonneDeiDocumenti();
  } catch (error) {
    state.loading = false;
    state.review = null;
    state.runtimeError = error.message;
    render();
  }
}

// Which columns the current comparison actually read from each document.
// Reloaded only when the source recompute changes — the only thing that can
// change them — never on the pipeline's per-second polling redraw. If the
// call fails, the document cards just stay as they were: this panel explains,
// it isn't a requirement to keep working.
async function caricaColonneDeiDocumenti() {
  if (mode === "demo" || state.colonneDocumenti.caricando) return;
  const runId = String(state.review?.run?.pipelineRunId || "");
  if (state.colonneDocumenti.caricate && state.colonneDocumenti.runId === runId) return;
  state.colonneDocumenti.caricando = true;
  try {
    const payload = await requestJson(API.schemasColumns);
    const perNome = {};
    for (const documento of asArray(payload?.documents)) {
      const nome = String(documento?.fileName || "");
      if (nome) perNome[nome] = documento;
    }
    state.colonneDocumenti = {
      caricate: true,
      caricando: false,
      runId: String(payload?.runId || runId),
      perNome,
      motivo: String(payload?.motivo || ""),
      errore: "",
    };
  } catch (error) {
    state.colonneDocumenti.caricate = true;
    state.colonneDocumenti.caricando = false;
    state.colonneDocumenti.runId = runId;
    state.colonneDocumenti.perNome = {};
    state.colonneDocumenti.motivo = "";
    state.colonneDocumenti.errore = error.message || "Non sono riuscito a leggere le colonne usate.";
  }
  render();
}

// Starts a new comparison: clears documents and the current comparison and
// goes back to step 1.
//
// What is NOT cleared is decided by the backend (`nuova_comparazione`), not
// this function: confirmations, learned schemas, pending orders and past
// compilations all survive. This function only resets what lives in the page
// itself — filters, summary, compile result, undo banners — since
// `loadReview()` doesn't touch any of that, and without resetting it the new
// comparison would start with the previous week's filters still applied.
async function cominciaNuovaComparazione() {
  if (state.nuovaComparazione.inCorso) return;
  if (mode === "demo") {
    state.nuovaComparazione.chiedendo = false;
    showToast("Nell’esempio non c’è niente da svuotare.", "error");
    render();
    return;
  }
  // Same guard as the backend. Needed here too: the response arrives later,
  // and until then the button would already look like it succeeded.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: la comparazione nuova si comincia quando ha finito.", "error");
    return;
  }
  state.nuovaComparazione.inCorso = true;
  // The queued autosave must be cancelled BEFORE requesting the reset. The
  // page autosaves 450ms after the last edit; a save that fires after the
  // reset targets a comparison that no longer exists, gets correctly
  // rejected by the service (it must not recreate last week's quantities),
  // and shows the user a "save failed" error for an action they took
  // deliberately.
  window.clearTimeout(state.saveTimer);
  const c_eranoModificheDaSalvare = state.dirty;
  state.dirty = false;
  state.savedVersion = state.saveVersion;
  render();
  try {
    const esito = await requestJson(API.comparazioneNuova, { method: "POST", body: JSON.stringify({}) });
    state.nuovaComparazione = { chiedendo: false, inCorso: false };
    // `state.json` no longer exists on disk: if this tab kept declaring the
    // old version, the first save after the reset would get rejected as
    // stale.
    state.stateVersion = 0;
    state.saveVersion = 0;
    state.savedVersion = 0;
    state.dirty = false;
    state.currentStep = 1;
    state.filters = { ...state.filters, search: "", type: "all", status: "all", page: 1, promotionId: null, promotionQuery: "", sort: "gestionale" };
    state.summary = { grouping: "supplier", supplierId: "all", sort: "name" };
    state.compileResult = null;
    state.compileFailure = null;
    state.acceptBelowThreshold = false;
    state.exclusionUndo = null;
    state.rimozioneUndo = null;
    state.azzeramentoUndo = null;
    state.supplierMoveUndo = null;
    state.supplierMove = emptySupplierMove();
    state.pendingFiles = [];
    state.fileScartati = [];
    state.uploadMessage = "";
    state.compilazioni = { ...state.compilazioni, aperta: false };
    // The pipeline state is reset to whatever the service sends back —
    // "idle", with no "documents changed" banner: there's no previous
    // comparison left for prices to be stale against.
    state.pipeline = { ...state.pipeline, stato: esito?.pipeline || null, chiesto: false, errore: "", contattoPerso: "", controlliPersi: 0 };
    await loadReview();
    // And the pending-orders question, whose moment is now.
    loadPendingOrders();
    showToast(esito?.message || "Comparazione nuova.");
  } catch (error) {
    state.nuovaComparazione.inCorso = false;
    // Nothing started: documents and choices are still in place, so
    // whatever needed saving still needs saving.
    if (c_eranoModificheDaSalvare) scheduleSave();
    showToast(error.message || "La comparazione nuova non è cominciata.", "error");
    render();
  }
}

// The list is a convenience, not a requirement: if the call fails, the page
// just has no reminders at the top, and everything else still works.
async function loadPendingOrders() {
  if (mode === "demo" || state.history.loading) return;
  state.history.loading = true;
  state.history.errore = "";
  try {
    const payload = await requestJson(API.historyPending);
    state.history.pending = normalizePendingOrders(payload?.pending);
  } catch (error) {
    // An empty list and a failed request must stay distinguishable, not
    // both silently show an empty panel: they're opposites — "nothing
    // pending" vs. "failed to find out" — and conflating them risks
    // reordering stock that's already on its way.
    state.history.pending = [];
    state.history.errore = `Non sono riuscito a leggere gli ordini già fatti: ${error.message}`;
  } finally {
    state.history.loading = false;
    // The response arrives after the page is already rendered: redraws only
    // where needed, without stealing focus from someone typing in search.
    // Also runs on step 1: since "start new comparison" was added, that's
    // the actual moment to ask "did the stock arrive?" — right when a new
    // week begins.
    if (state.currentStep <= 2) rerenderPreservingFocus();
  }
}

// Compilation history: fetched on entering step 3, and again after every
// successful compile, since the one just made becomes its first entry.
// A failure here stays inside its own panel (state.compilazioni.errore) and
// never touches state.runtimeError: history is a convenience for finding
// past price lists, not a precondition for compiling, and breaking step 3
// over it would be worse than just missing the list.
async function loadCompilazioni() {
  if (mode === "demo" || state.compilazioni.inCorso) return;
  state.compilazioni.inCorso = true;
  state.compilazioni.errore = "";
  render();
  try {
    const payload = await requestJson(API.ordini);
    if (payload?.ok === false) throw new Error(payload.message || "Lo storico delle compilazioni non è disponibile.");
    state.compilazioni.elenco = asArray(payload?.compilazioni);
    state.compilazioni.caricate = true;
  } catch (error) {
    // Discards the stale list rather than keeping it: showing compilations
    // that weren't just re-read from disk would be misleading.
    state.compilazioni.elenco = [];
    state.compilazioni.errore = `Storico delle compilazioni non letto: ${error.message}`;
  } finally {
    state.compilazioni.inCorso = false;
    // The response arrives after the page is already rendered: redraws
    // without stealing focus from someone using the summary filters.
    rerenderPreservingFocus();
  }
}

// The discount is applied by the service: discounted prices and new
// assignments come from re-fetching the comparison, never from a
// recalculation done here. A second pricing engine in the browser would
// mean two different truths for the same row, and the second one would be
// the one shown next to the total.
async function applicaScontoFornitore(supplierId, valore) {
  if (!supplierId || mode === "demo") return;
  const percent = Math.min(99, Math.max(0, finiteNumber(valore, 0)));
  try {
    // Saves BEFORE the request, for the same reason spelled out in full in
    // `abbinaLaRiga`: `loadReview()` below re-fetches the comparison and
    // REPLACES the one in the page, so a quantity typed less than 450ms
    // ago — or one still unsaved after a failed save — would be silently
    // lost with it, producing a wrong order with no indication anything
    // went wrong. With a clean state, `saveState()` resolves immediately.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const esito = await requestJson(API.supplierDiscount, {
      method: "POST",
      body: JSON.stringify({ supplierId, percent }),
    });
    await loadReview();
    // The service counts how many products the discount reassigned to this
    // supplier, the only feedback that entering a percentage actually moved
    // something. Read AFTER `loadReview`, so the toast's supplier name
    // reflects the just-updated comparison.
    const spostati = Math.max(0, Math.trunc(finiteNumber(esito?.reassigned, 0)));
    if (spostati) {
      showToast(spostati === 1
        ? `1 prodotto è passato a ${supplierName(supplierId)}.`
        : `${formatInteger(spostati)} prodotti sono passati a ${supplierName(supplierId)}.`);
    } else if (percent > 0) {
      showToast(`Sconto applicato a ${supplierName(supplierId)}: nessun prodotto ha cambiato fornitore.`);
    }
  } catch (error) {
    showToast(error.message || "Lo sconto non è stato applicato.", "error");
  }
}

async function deleteCompilation(cartella) {
  if (!cartella || state.compilazioni.eliminando) return;
  state.compilazioni.eliminando = cartella;
  state.compilazioni.errore = "";
  render();
  try {
    // Saves BEFORE the request, for the same reason spelled out in full in
    // `abbinaLaRiga`: once the compilation is deleted, the page re-fetches
    // the comparison and REPLACES the one in the page, so a quantity typed
    // less than 450ms ago — or one still unsaved after a failed save —
    // would be silently lost with it, producing a wrong order with no
    // indication anything went wrong. With a clean state, `saveState()`
    // resolves immediately.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const result = await requestJson(API.ordiniDelete, {
      method: "POST",
      body: JSON.stringify({ cartella }),
    });
    if (result?.ok === false) throw new Error(result.message || "La compilazione non è stata eliminata.");
    state.compilazioni.elenco = asArray(result.compilazioni);
    state.compilazioni.caricate = true;
    state.compilazioni.confermaElimina = "";
    if (state.compileResult?.cartella === cartella) state.compileResult = null;
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    await loadPendingOrders();
    const removed = Math.max(0, Math.trunc(finiteNumber(result.promemoriaRimossi, 0)));
    showToast(removed
      ? `Compilazione eliminata insieme a ${removed === 1 ? "1 promemoria" : `${formatInteger(removed)} promemoria`}.`
      : "Compilazione eliminata.");
  } catch (error) {
    state.compilazioni.errore = `Compilazione non eliminata: ${error.message}`;
    showToast("Compilazione non eliminata.", "error");
  } finally {
    state.compilazioni.eliminando = "";
    render();
  }
}

function applyPipelineAfterInputChange(pipeline) {
  fermaPollingPipeline();
  resetSchemaMapping();
  state.pipeline.errore = "";
  state.pipeline.stato = pipeline && typeof pipeline === "object" ? pipeline : null;
  const stato = String(state.pipeline.stato?.stato || "");
  // IN_ATTESA now covers two cases: "nothing has ever run" and "stale
  // comparison, documents have changed". Neither deserves the progress bar
  // — turned off by renderAvanzamentoPipeline() based on this status — while
  // the second is what renderCambiamentoDocumenti() reports by reading
  // `state.pipeline.stato`, which is why the status is kept either way.
  state.pipeline.chiesto = Boolean(stato && stato !== "IN_ATTESA");
  if (stato === "IN_CORSO") pianificaControlloPipeline();
}

async function deleteUploadedList(name) {
  if (!name || state.uploadDeletion.deleting) return;
  // Removing a price list while the pipeline is running would change the
  // result it's computing: the action is already disabled in the UI, and
  // this is the backstop.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: i listini si eliminano appena ha finito.", "error");
    return;
  }
  state.uploadDeletion.deleting = name;
  state.runtimeError = "";
  render();
  try {
    // Saves BEFORE the request, for the same reason spelled out in full in
    // `abbinaLaRiga`: once the price list is deleted, the page re-fetches
    // the comparison and REPLACES the one in the page, so a quantity typed
    // less than 450ms ago — or one still unsaved after a failed save —
    // would be silently lost with it, producing a wrong order with no
    // indication anything went wrong. With a clean state, `saveState()`
    // resolves immediately.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const result = await requestJson(API.uploadDelete, {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    if (result?.ok === false) throw new Error(result.message || "Il listino non è stato eliminato.");
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    applyPipelineAfterInputChange(result.pipeline);
    state.uploadDeletion.confirming = "";
    showToast(String(result.message || "Listino eliminato."));
  } catch (error) {
    state.runtimeError = `Listino non eliminato: ${error.message}`;
    showToast("Listino non eliminato.", "error");
  } finally {
    state.uploadDeletion.deleting = "";
    render();
  }
}

// "Not the same item", and the way back. The negative counterpart of the
// confirmation checkbox, sharing the same store: the confirmations database
// (`app/conferme.py`), as a row with `accettata` false. It doesn't touch any
// new `state.json` key and doesn't need to survive a recompute on its own —
// it survives because it's tied to the ITEM, not the row.
//
// Saves BEFORE the response, like the candidate answer: this button reloads
// the comparison, and whatever the user typed so far must not be lost by
// pressing it. The save succeeds even with confirmation still missing —
// `save_state` doesn't treat it as blocking — so the button can't refuse to
// work in exactly the case it exists for. The "yes" to the same question is
// given the same way.
//
// "Yes" to the same question is a button too, not a checkbox with a
// deferred autosave: a checkbox would write the value into state and wait
// for the delayed save, giving no feedback and no explicit wait, and if the
// save failed it would stay checked claiming an answer that never made it.
// The button acts, waits, and reports the outcome, reverting on failure
// instead of staying drawn.
//
// The answer itself lands in `product.confirmed` in the snapshot, written
// to the store by `_ricorda_le_conferme` on save, and read by compilation
// as the key it checks — the save is immediate and awaited, same as the
// "no" path above.
async function confermaLAbbinamento(productId, confermato) {
  const answerKey = `${productId}:conferma`;
  if (!productId || state.matches.answering) return;
  const product = findProduct(productId);
  if (!product) return;
  const prima = Boolean(product.confirmed);
  if (prima === Boolean(confermato)) return;
  state.matches.answering = answerKey;
  product.confirmed = Boolean(confermato);
  // Marks "needs saving" and saves immediately. The timer `scheduleSave()`
  // leaves behind must be cancelled: it fires after 450ms, and if the save
  // below fails and the error branch reverts the flag, that timer would
  // still resend the just-reverted value — recording an answer the user saw
  // fail.
  scheduleSave();
  window.clearTimeout(state.saveTimer);
  rerenderPreservingFocus();
  try {
    const saved = await saveState();
    if (!saved) throw new Error(state.runtimeError || "Le modifiche non sono state salvate.");
    // Re-fetches the comparison: the confirmation just written comes back
    // with its date, which the card then shows in place of the question.
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    showToast(confermato
      ? "Conferma registrata: vale per l’articolo, anche con il listino della settimana prossima."
      : "Conferma tolta: la domanda è di nuovo aperta.");
  } catch (error) {
    // Reverts the flag. Leaving it as-is would mean a checkbox claiming
    // "answered" over an answer that never actually landed — exactly the
    // old checkbox's failure mode.
    //
    // Reverted on the product reference already held, not one looked up
    // again: between the start of this call and here, the comparison may
    // have been re-fetched (happens on every finished recompute), and
    // `findProduct` would then resolve to a different object, or nothing.
    // If the comparison changed, this revert is moot and harmless; if it
    // didn't, this is the only reference that still works.
    product.confirmed = prima;
    showToast(`Risposta non registrata: ${error.message}`, "error");
  } finally {
    state.matches.answering = "";
    // `rerenderPreservingFocus`, not `render`: this answer leaves the card
    // where it is — the button becomes "change my mind" and keeps the same
    // `focus-key` — so focus can be restored to it. Pressing this button
    // must not throw away keyboard focus. The two counterparts that answer
    // "no" use `render()` for the opposite reason: those reload the
    // comparison and the pressed button disappears.
    rerenderPreservingFocus();
  }
}

async function rifiutaLAbbinamento(productId, supplierId, rifiutata) {
  const answerKey = `${productId}:${supplierId}:rifiuto`;
  if (!productId || !supplierId || state.matches.answering) return;
  state.matches.answering = answerKey;
  render();
  try {
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const result = await requestJson(API.matchRifiuta, {
      method: "POST",
      body: JSON.stringify({
        runId: state.review?.run?.id || "",
        productId,
        supplierId,
        rifiutata: Boolean(rifiutata),
      }),
    });
    if (result?.ok === false) throw new Error(result.message || "La risposta non è stata registrata.");
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    showToast(String(result.message || "Risposta registrata.") + doveFinisceIlProdotto(productId, rifiutata));
  } catch (error) {
    showToast(`Risposta non registrata: ${error.message}`, "error");
  } finally {
    state.matches.answering = "";
    render();
  }
}

// After the last rejection, the product's card DISAPPEARS: the "to confirm"
// list is where questions get answered, and a product left with no
// remaining supplier is no longer a question, so it drops out of that
// filter. With nothing pointing to where it went, only the filter counts
// shift and the card is simply gone.
//
// This function names the product's new location right here, at the moment
// the location changes — not as static text placed somewhere else on the
// card ahead of time.
//
// The new location is "you answered no", not "no supplier has it": pointing
// to the wrong filter is worse than saying nothing.
function doveFinisceIlProdotto(productId, rifiutata) {
  if (!rifiutata) return "";
  const prodotto = asArray(state.review?.products).find((item) => item.id === productId);
  if (!prodotto || !nessunaOffertaUtilizzabile(prodotto) || orderQuantity(prodotto) <= 0) return "";
  return " Questo prodotto non ha più nessun fornitore che tu possa scegliere:"
    + " lo trovi con il filtro «Hai risposto no», con la sua quantità.";
}

async function answerRejectedCandidate(productId, supplierId, candidateKey, accepted) {
  const answerKey = `${productId}:${supplierId}:${candidateKey}`;
  if (!productId || !supplierId || !candidateKey || state.matches.answering) return;
  state.matches.answering = answerKey;
  render();
  try {
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const result = await requestJson(API.matchAnswer, {
      method: "POST",
      body: JSON.stringify({
        runId: state.review?.run?.id || "",
        productId,
        supplierId,
        candidateKey,
        accepted: Boolean(accepted),
      }),
    });
    if (result?.ok === false) throw new Error(result.message || "La risposta non è stata registrata.");
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    showToast(String(result.message || "Risposta registrata."));
  } catch (error) {
    showToast(`Risposta non registrata: ${error.message}`, "error");
  } finally {
    state.matches.answering = "";
    render();
  }
}

// The answer applies to the whole order from that supplier: partial
// deliveries aren't modeled. Three possible answers — received, not yet
// (the question comes back next week), or will never arrive (closed without
// claiming the stock arrived). All purely informational: none touch
// quantity, prices or the selected supplier.
async function answerPendingOrder(orderId, received, { closed = false } = {}) {
  if (!orderId || state.history.answering) return;
  const entry = asArray(state.history.pending).find((candidate) => candidate.orderId === orderId);
  if (!entry) return;
  state.history.answering = orderId;
  render();
  try {
    if (mode !== "demo") {
      const result = await requestJson(API.historyAnswer, {
        method: "POST",
        body: JSON.stringify(closed ? { orderId, closed: true } : { orderId, received: Boolean(received) }),
      });
      if (result?.ok === false) throw new Error(result.message || "La risposta non è stata registrata.");
      if (Array.isArray(result?.pending)) state.history.pending = normalizePendingOrders(result.pending);
    }
    // In every case, the question isn't asked again in this session.
    state.history.answered.add(orderId);
    if (closed) {
      forgetPendingOrder(orderId);
      showToast(`Ordine di ${entry.supplierName || entry.supplier} chiuso: non verrà più chiesto.`);
    } else if (received) {
      forgetPendingOrder(orderId);
      showToast(`Merce di ${entry.supplierName || entry.supplier} segnata come ricevuta.`);
    } else {
      showToast("Va bene: l’ordine resta in attesa, la domanda torna fra una settimana.");
    }
  } catch (error) {
    showToast(`Risposta non registrata: ${error.message}`, "error");
  } finally {
    state.history.answering = "";
    render();
  }
}

// Stock arrived: "already ordered" notices for that order no longer apply.
function forgetPendingOrder(orderId) {
  for (const product of asArray(state.review?.products)) {
    if (!Array.isArray(product.pendingOrders) || !product.pendingOrders.length) continue;
    product.pendingOrders = product.pendingOrders.filter((pending) => pending.orderId !== orderId);
  }
}

function updateSaveStatus(message) {
  saveStatusElement.textContent = message;
}

function showToast(message, kind = "success") {
  const toast = document.createElement("div");
  toast.className = `toast${kind === "error" ? " is-error" : ""}`;
  toast.textContent = message;
  toastRegionElement.append(toast);
  window.setTimeout(() => toast.remove(), 3600);
}

function applyPromotionUpdate(result) {
  if (!state.review || !result || typeof result !== "object") return false;
  const promotionStates = result.promotionStates || {};
  if (result.promotionSummary && typeof result.promotionSummary === "object") {
    state.review.promotionSummary = result.promotionSummary;
  }
  // Summary totals — and the rounding difference between header and line
  // items — are recomputed by the local service on every save: the browser
  // isn't authoritative on prices and just keeps whatever it's sent.
  if (result.orderSummary && typeof result.orderSummary === "object") {
    state.review.orderSummary = normalizeOrderSummary(result.orderSummary);
  }
  const updatePromotion = (promotion) => {
    const nextState = promotionStates[String(promotion?.id || "")];
    if (nextState) promotion.state = nextState;
  };
  state.review.promotions.forEach(updatePromotion);
  for (const product of state.review.products) {
    product.promotions.forEach(updatePromotion);
    for (const offer of product.offers) {
      offer.promotions.forEach(updatePromotion);
      if (offer.promotions.length) {
        offer.promotion = offer.promotions.map(promotionText).filter(Boolean).join(" · ");
      }
    }
  }
  return Boolean(result.promotionSummary || result.orderSummary || Object.keys(promotionStates).length);
}

// Adopts the new state version from whichever response carries it. Called by
// `requestJson` on every successful response, from one place: a hand-picked
// list of "which endpoints rewrite state" was too easy to miss entries in.
// Only moves forward, never back, so the order responses arrive in doesn't
// matter.
function applyStateVersion(result) {
  const versione = finiteNumber(result?.stateVersion, NaN);
  if (Number.isFinite(versione) && versione > state.stateVersion) {
    state.stateVersion = Math.trunc(versione);
  }
}

function scheduleSave() {
  state.dirty = true;
  state.saveVersion += 1;
  state.runtimeError = "";
  if (mode === "demo") {
    updateSaveStatus("Modifiche dell’esempio non salvate");
    return;
  }
  updateSaveStatus("Modifiche da salvare…");
  window.clearTimeout(state.saveTimer);
  state.saveTimer = window.setTimeout(() => saveState(), 450);
}

async function saveState() {
  if (mode === "demo") return true;
  if (!state.review) return true;
  if (state.savePromise) {
    const previousSucceeded = await state.savePromise;
    if (!previousSucceeded) return false;
    return state.savedVersion >= state.saveVersion ? true : saveState();
  }
  if (!state.dirty) return true;

  state.saving = true;
  updateSaveStatus("Salvataggio…");
  const version = state.saveVersion;
  const payload = snapshot();
  const request = (async () => {
    try {
      const result = await requestJson(API.state, {
        method: "PUT",
        body: JSON.stringify(payload),
      });
      const promotionsChanged = applyPromotionUpdate(result);
      state.savedVersion = Math.max(state.savedVersion, version);
      state.dirty = state.savedVersion < state.saveVersion;
      state.runtimeError = "";
      state.tentativiDiSalvataggio = 0;
      updateSaveStatus(state.dirty ? "Modifiche da salvare…" : "Tutto salvato");
      if (promotionsChanged && state.currentStep === 3) render();
      return true;
    } catch (error) {
      state.dirty = true;
      state.runtimeError = `Salvataggio non riuscito: ${error.message}`;
      updateSaveStatus("Salvataggio non riuscito");
      // Three attempts, then stop: an offline service won't come back
      // because of infinite retries, and a page that keeps polling every
      // five seconds forever is a bug of its own. After the third failure,
      // the retry action stays at the top instead.
      state.tentativiDiSalvataggio += 1;
      if (state.tentativiDiSalvataggio <= 3) {
        window.clearTimeout(state.saveTimer);
        state.saveTimer = window.setTimeout(() => { if (state.dirty) saveState(); }, 5000);
      }
      renderGlobalMessage();
      return false;
    }
  })();
  state.savePromise = request;
  try {
    return await request;
  } finally {
    if (state.savePromise === request) {
      state.savePromise = null;
      state.saving = false;
    }
  }
}

function fileStatus(file) {
  if (["error", "failed", "ambiguous"].includes(file.status)) {
    return { label: "Da correggere", tone: "danger" };
  }
  if (["warning", "review", "changed"].includes(file.status)) {
    return { label: "Da verificare", tone: "warning" };
  }
  if (["processing", "pending", "queued"].includes(file.status)) {
    return { label: "In analisi", tone: "info" };
  }
  return { label: "Pronto", tone: "success" };
}

function issueTone(issue) {
  if (issue.blocking || issue.severity === "error") return "danger";
  if (issue.severity === "info") return "info";
  // renderAlert already draws the checkmark for the "success" tone and the
  // stylesheet already has .alert--success: only a way to reach it was
  // missing. Used on the Settings page, where "the API key works" is the
  // one green response.
  if (issue.severity === "success") return "success";
  return "warning";
}

// collectIssues() scans every product and is called up to ~40 times per page
// render: the result is computed once per render cycle and cached.
// INVARIANT: any code that changes quantity, supplier, confirmations or
// exclusions must invalidate this cache, either directly or via render().
// A caller reading issues outside the render cycle must call
// invalidateIssues() first.
let issuesCache = null;
let issuesByProductCache = null;

function invalidateIssues() {
  issuesCache = null;
  issuesByProductCache = null;
}

function collectIssues() {
  if (issuesCache) return issuesCache;
  if (!state.review) return [];
  const issues = [...state.review.warnings];

  for (const product of state.review.products) {
    if (orderQuantity(product) <= 0) continue;
    issues.push(...product.warnings);

    const offer = selectedOffer(product);
    // "You haven't chosen yet" and "there's nothing to choose from" are
    // different states. A product no supplier can fulfill must not block
    // compilation or force the quantity to zero — that quantity is exactly
    // what's needed to source the item elsewhere. The local service accepts
    // this case (`nessuna_offerta_utilizzabile`, server.py): the quantity
    // stays, the product is left out of every supplier's list, and it
    // appears in the "to be sourced" list instead. Its own card already
    // shows the SENZA_OFFERTA_UTILIZZABILE warning explaining why.
    if (!offer && !nessunaOffertaUtilizzabile(product)) {
      issues.push(normalizeIssue({
        id: `${product.id}-no-selection`,
        code: "FORNITORE_MANCANTE",
        severity: "error",
        blocking: true,
        productId: product.id,
        title: "Fornitore mancante",
        message: `${product.name}: scegli un fornitore fra quelli disponibili oppure imposta la quantità a zero.`,
      }, `${product.id}-no-selection`));
    }

    if (confirmationRequired(product) && !product.confirmed) {
      issues.push(normalizeIssue({
        id: `${product.id}-confirmation`,
        code: "CONFERMA_RICHIESTA",
        severity: "error",
        blocking: true,
        productId: product.id,
        title: "Conferma richiesta",
        message: `${product.name}: ${confirmationMessageFor(product)}`,
      }, `${product.id}-confirmation`));
    }
  }

  const totals = supplierTotals();
  for (const entry of totals) {
    if (entry.total > 0 && entry.minimumOrder > 0 && entry.total < entry.minimumOrder) {
      issues.push(normalizeIssue({
        id: `minimum-${entry.supplier.id}`,
        severity: "warning",
        blocking: false,
        title: `Minimo d’ordine di ${entry.supplier.name} non raggiunto`,
        message: `Mancano ${formatEuro(entry.minimumOrder - entry.total)} al minimo d’ordine di ${entry.supplier.name} (${formatEuro(entry.minimumOrder)}). Puoi comunque preparare il listino per verificarlo.`,
      }, `minimum-${entry.supplier.id}`));
    }
  }

  const unique = new Map();
  for (const issue of issues) {
    if (!unique.has(issue.id)) unique.set(issue.id, issue);
  }
  issuesCache = [...unique.values()];
  return issuesCache;
}

function issuesForProduct(product) {
  const issues = collectIssues();
  if (!issuesByProductCache) {
    issuesByProductCache = new Map();
    for (const issue of issues) {
      if (!issue.productId) continue;
      if (!issuesByProductCache.has(issue.productId)) issuesByProductCache.set(issue.productId, []);
      issuesByProductCache.get(issue.productId).push(issue);
    }
  }
  return issuesByProductCache.get(product.id) || [];
}

function supplierTotals() {
  if (!state.review) return [];
  const totals = new Map(state.review.suppliers.map((supplier) => [supplier.id, {
    supplier,
    total: 0,
    lines: 0,
    units: 0,
    products: [],
    minimumOrder: supplier.minimumOrder,
  }]));

  for (const product of state.review.products) {
    const quantity = orderQuantity(product);
    const offer = selectedOffer(product);
    if (quantity <= 0 || !offer) continue;
    if (!totals.has(offer.supplierId)) {
      const supplier = { id: offer.supplierId, name: offer.supplierName, minimumOrder: 0 };
      totals.set(offer.supplierId, { supplier, total: 0, lines: 0, units: 0, products: [], minimumOrder: 0 });
    }
    const entry = totals.get(offer.supplierId);
    const calculation = offerCalculation(product, offer, quantity);
    entry.total += calculation.total;
    entry.lines += 1;
    entry.units += calculation.orderUnits;
    entry.products.push({ product, offer, quantity, calculation, subtotal: calculation.total });
  }

  return [...totals.values()];
}

function allOrderTotal() {
  return supplierTotals().reduce((total, entry) => total + entry.total, 0);
}

function renderBadge(label, tone = "neutral") {
  return `<span class="badge badge--${escapeHtml(tone)}">${escapeHtml(label)}</span>`;
}

// Warnings that name a product-level filter (e.g. "9 products have a
// supplier proposal to confirm or reject") carry an action that jumps
// straight to that filter, instead of naming it and leaving the user to
// find the right entry in a dropdown after switching pages. This matters
// most on the summary page: "Confirmation required · blocking" explains why
// compilation won't start, and the action gives a path to unblock it.
//
// The action is picked from `code`, which the service already sends and
// which the two warnings created in the browser also carry: no separate
// table of titles to keep in sync with wording. A warning with an unknown
// code just has no action.
const COMANDI_DEGLI_AVVISI = {
  RIFIUTI_CON_CANDIDATO_FORTE: { etichetta: "Vai ai prodotti da confermare", filtro: "to-confirm" },
  CONFERMA_RICHIESTA: { etichetta: "Vai alle conferme", filtro: "to-confirm" },
  PRODOTTI_SENZA_OFFERTA: { etichetta: "Vai a quelli che nessuno ha", filtro: "missing" },
  FORNITORE_MANCANTE: { etichetta: "Vai ai prodotti da ordinare", filtro: "ordered" },
};

function comandoDellAvviso(issue) {
  return COMANDI_DEGLI_AVVISI[issue?.code] || null;
}

function renderComandoDellAvviso(issue) {
  const comando = comandoDellAvviso(issue);
  if (!comando) return "";
  return `<button type="button" class="button button--secondary button--small alert__comando" data-action="vai-al-filtro" data-filtro="${escapeHtml(comando.filtro)}">${escapeHtml(comando.etichetta)}</button>`;
}

// `conComando` defaults to off: the same warnings also appear on a single
// product's own card in step 2, where the action would just navigate to
// where the user already is, next to the actual answer button. Only the
// page-level panels in `renderAvvisi` turn it on.
function renderAlert(issue, { conComando = false } = {}) {
  const tone = issueTone(issue);
  const icon = tone === "danger" ? "!" : tone === "success" ? "✓" : tone === "info" ? "i" : "!";
  return `
    <div class="alert alert--${tone}" role="${tone === "danger" ? "alert" : "status"}">
      <span class="alert__icon" aria-hidden="true">${icon}</span>
      <div class="alert__body">
        <strong>${escapeHtml(issue.title)}${issue.blocking ? " · bloccante" : ""}</strong>
        <p>${escapeHtml(issue.message)}</p>
        ${conComando ? renderComandoDellAvviso(issue) : ""}
      </div>
    </div>`;
}

// How many identical warnings it takes before grouping them is worth it
// instead of printing each one. Two are two lines; three or more become a
// wall of text.
const AVVISI_DA_RAGGRUPPARE = 3;

// With dozens of warnings before the summary, printing each one individually
// meant reading through all of them just to reach the order numbers, even
// though most shared the same title and differed only in which product they
// were about.
//
// Grouped by title, which the service already writes: no separate table of
// codes to keep aligned with wording, and a new title groups itself
// automatically. Nothing is dropped: every message still appears, inside its
// group.
function raggruppaAvvisi(issues) {
  const perTitolo = new Map();
  for (const issue of issues) {
    if (!perTitolo.has(issue.title)) perTitolo.set(issue.title, []);
    perTitolo.get(issue.title).push(issue);
  }
  return [...perTitolo.entries()].map(([titolo, voci]) => ({
    titolo,
    voci,
    raggruppato: voci.length >= AVVISI_DA_RAGGRUPPARE,
  }));
}

function renderGruppoDiAvvisi(gruppo, chiave) {
  const primo = gruppo.voci[0];
  const tono = issueTone(primo);
  const icona = tono === "danger" ? "!" : tono === "info" ? "i" : "!";
  const intestazione = `
    <span class="alert__icon" aria-hidden="true">${icona}</span>
    <span class="alert__body">
      <strong>${escapeHtml(gruppo.titolo)}${primo.blocking ? " · bloccante" : ""}</strong>
      ${renderBadge(String(gruppo.voci.length), tono === "danger" ? "danger" : "warning")}
    </span>`;
  const comando = renderComandoDellAvviso(primo);
  const elenco = `<ul class="alert__elenco">${gruppo.voci.map((issue) => `<li>${escapeHtml(issue.message)}</li>`).join("")}</ul>${comando ? `<div class="alert__comandi">${comando}</div>` : ""}`;
  // A blocking group can't be collapsed: it's the reason compilation won't
  // start, and hiding it would hide the one thing left to fix.
  if (primo.blocking) {
    return `<div class="alert alert--${tono} alert--gruppo" role="alert">${intestazione}${elenco}</div>`;
  }
  return `
    <details class="alert alert--${tono} alert--gruppo" ${apribile(`avvisi:${chiave}:${gruppo.titolo}`)}>
      <summary>${intestazione}</summary>
      ${elenco}
    </details>`;
}

function renderAvvisi(issues, chiave) {
  return raggruppaAvvisi(issues)
    .map((gruppo) => (gruppo.raggruppato
      ? renderGruppoDiAvvisi(gruppo, chiave)
      // Two warnings sharing a title are two separate panels, but the
      // filter action is the same: shown only on the first one, not both.
      : gruppo.voci.map((issue, indice) => renderAlert(issue, { conComando: indice === 0 })).join("")))
    .join("");
}

// Warnings about the source documents — an unread price list, a supplier
// left out of the comparison — must surface before the user starts choosing,
// not only in the step-3 panel after products and suppliers are already
// picked: a supplier could otherwise be missing for a whole step with
// nothing saying so.
function avvisiDelConfronto() {
  return (state.review?.warnings || []).filter((issue) => !issue.productId);
}

// Warnings about PRODUCTS, not documents: answered from the step-2 list, so
// that's where they belong. Everything else is about uploaded files and
// belongs on step 1, where files are managed.
//
// A code this set doesn't know falls back to the document group on step 1:
// that page is always visible, so a new warning code doesn't silently
// disappear before anyone notices it.
const AVVISI_DEI_PRODOTTI = new Set([
  "RIFIUTI_CON_CANDIDATO_FORTE",
  "PRODOTTI_SENZA_OFFERTA",
  "DECISIONI_AI_SCARTATE",
  "ORDINI_SCADUTI_SENZA_RISPOSTA",
]);

// Showing the identical warning panel, with the identical title, on every
// step made repeated warnings lose weight: seeing the same thing three times
// trains a user to stop reading it.
//
// Each warning now lives where something can be done about it: documents on
// step 1, products on step 2 — which also carries the action linking to the
// right filter — and step 3 shows all of them again, since it's the last
// point before the order is written. Three distinct titles, each naming
// what its panel covers.
//
// A BLOCKING warning is the exception and stays on both pages: it stops
// compilation and must not be hidden by this grouping.
// The comparison-level warnings the panel at the top of step 1 is actually
// printing right now. This is the set relevant to someone trying not to
// repeat them, and it isn't "every warning in the comparison": while the
// guided column mapping is open, only blocking ones remain.
function avvisiVisibiliInCima() {
  const soloBloccanti = schemaMappingRequired();
  return avvisiDelConfronto().filter(
    (issue) => issue.blocking || (!soloBloccanti && !AVVISI_DEI_PRODOTTI.has(issue.code)),
  );
}

function renderSourceWarnings(pagina) {
  const deiProdotti = pagina === 2;
  // While the guided column mapping is open, the page has one question —
  // where are the columns — and these warnings describe the PREVIOUS
  // comparison, not the one being rebuilt. Blocking ones stay, since they
  // stop compilation and can't be hidden; the rest reappear on their own
  // once the recompute finishes and rewrites `review_data.json`.
  const soloBloccanti = !deiProdotti && schemaMappingRequired();
  const warnings = avvisiDelConfronto().filter(
    (issue) => issue.blocking || (!soloBloccanti && AVVISI_DEI_PRODOTTI.has(issue.code) === deiProdotti),
  );
  if (!warnings.length) return "";
  const blocking = warnings.filter((issue) => issue.blocking).length;
  const titolo = deiProdotti ? "Da sapere sui prodotti" : "Da sapere sui documenti caricati";
  return `
    <section class="panel">
      <div class="panel__header">
        <div><h3>${escapeHtml(titolo)}</h3></div>
        ${renderBadge(contati(warnings.length, "avviso", "avvisi"), blocking ? "danger" : "warning")}
      </div>
      ${renderAvvisi(warnings, deiProdotti ? "prodotti" : "documenti")}
    </section>`;
}

function renderHeader() {
  if (mode === "demo") {
    modeBadgeElement.className = "badge badge--warning";
    modeBadgeElement.textContent = "Esempio";
  } else if (state.review) {
    // run.status is already "ready" on first launch, with zero documents
    // uploaded: showing the green "data ready" badge then would tell a user
    // who hasn't done anything yet that everything is set. Green only once
    // a comparison genuinely exists.
    if (!confrontoDisponibile()) {
      modeBadgeElement.className = "badge badge--neutral";
      modeBadgeElement.textContent = "Da preparare";
    } else {
      modeBadgeElement.className = "badge badge--success";
      modeBadgeElement.textContent = state.review.run.status === "ready" ? "Dati pronti" : "Controllo in corso";
    }
  } else {
    modeBadgeElement.className = "badge badge--neutral";
    modeBadgeElement.textContent = state.loading ? "Caricamento" : "Non connesso";
  }
}

// The green checkmark must mean "this step is actually done", not merely
// "you've been past this position" — a check based only on the current step
// number would mark step 1 done as soon as step 2 becomes reachable, even
// with nothing imported, and would lose the check on steps 2 and 3 when
// navigating back to step 1 despite quantities and suppliers already being
// set. For a non-technical user, the green check IS the claim that the step
// is in order.
//
// Each step's status is instead derived from facts the app already knows:
// step 1 is done once a comparison exists, step 2 once at least one product
// has a quantity. Step 3 has no equivalent "done" fact to declare — what
// happens there is compiling, and compilation has its own status panel — so
// it stays unchecked. The header badge already does the same job, staying
// non-green until a comparison genuinely exists.
function passoCompletato(id) {
  if (id === 1) return confrontoDisponibile();
  if (id === 2) return orderedProducts().length > 0;
  return false;
}

function renderStepper() {
  // Settings is effectively a fourth page, but the step bar lives outside
  // `#app` and isn't redrawn by the code that opens Settings, so it kept
  // showing whichever of the three steps was active before. Scrolling down,
  // the one fixed element on screen would say "Import data" while the user
  // is actually looking at declared item-equality rules — misleading for a
  // non-technical user. While Settings is open, the bar now gets a fourth
  // entry marked active instead; the three steps stay clickable, and
  // clicking one closes Settings, which is what `goToStep()` already does.
  const impostazioni = Boolean(state.impostazioni.aperta);
  const voci = STEPS.map((step) => {
    const active = !impostazioni && state.currentStep === step.id;
    const complete = Boolean(state.review) && passoCompletato(step.id);
    return `
      <li class="stepper__item${active ? " is-active" : ""}">
        <button class="stepper__button" type="button" data-step="${step.id}" ${state.review ? "" : "disabled"} ${active ? 'aria-current="step"' : ""}>
          <span class="stepper__label">${escapeHtml(step.label)}</span>
          ${complete ? '<span class="stepper__fatto">fatto</span>' : ""}
        </button>
      </li>`;
  });
  if (impostazioni) {
    voci.push(`
      <li class="stepper__item is-active">
        <button class="stepper__button" type="button" data-action="chiudi-impostazioni" aria-current="page">
          <span class="stepper__label">Impostazioni</span>
        </button>
      </li>`);
  }
  stepperElement.innerHTML = voci.join("");
}

function renderGlobalMessage() {
  if (!state.runtimeError) {
    globalMessageElement.innerHTML = "";
    return;
  }
  // The app retries a failed save automatically up to three times, and
  // shows this retry action only if that still fails. Without a manual
  // retry here, `state.dirty` would stay true with no further attempt
  // scheduled, leaving no way forward short of touching something else by
  // chance — and closing the page at that point would lose every edit made
  // since.
  const comando = state.dirty && !state.saving
    ? '<button type="button" class="button button--primary button--small" data-action="riprova-salvataggio">Riprova a salvare</button>'
    : "";
  globalMessageElement.innerHTML = renderAlert({
    title: "Non ci sono riuscito",
    message: state.runtimeError,
    severity: "error",
    blocking: true,
  }) + (comando ? `<div class="global-message__comando">${comando}</div>` : "");
}

function render() {
  invalidateIssues();
  renderHeader();
  renderStepper();
  renderGlobalMessage();

  if (state.loading) {
    appElement.innerHTML = `
      <div class="loading-card">
        <span class="spinner" aria-hidden="true"></span>
        <div><strong>Sto leggendo i tuoi documenti</strong><p>Controllo l’elenco del gestionale e i listini dei fornitori.</p></div>
      </div>`;
    return;
  }

  // Checked before the review state: Settings doesn't depend on having
  // products loaded, and the moment it's actually needed is exactly when
  // the AI stage has failed.
  if (state.impostazioni.aperta) {
    appElement.innerHTML = renderSettingsPage();
    // The key field's value never lives in the HTML. Setting it as a DOM
    // property instead lets it survive a redraw — so after a successful
    // connection test, "Save" works without re-pasting the key — without
    // the value ever appearing in the page source.
    const campoChiave = appElement.querySelector("[data-chiave-openrouter]");
    if (campoChiave) campoChiave.value = state.impostazioni.nuovaChiave;
    return;
  }

  if (!state.review) {
    appElement.innerHTML = `
      <div class="fatal-card">
        <div>
          <h2>Il programma non è partito</h2>
          <p>${escapeHtml(state.runtimeError || "Non sono riuscito a caricare il confronto dei listini.")}</p>
          <button type="button" class="button button--primary" data-action="retry">Riprova</button>
        </div>
      </div>`;
    return;
  }

  const renderers = [renderUploadStep, renderQuantityStep, renderCompileStep];
  // The order-column dialog can be opened from step 1, not only step 2: it's
  // rendered here rather than inside a single `renderX Step`, since the
  // three steps mount three different DOM roots, and a dialog mounted inside
  // just one would vanish when the step changes — while still open, with no
  // way to close it.
  appElement.innerHTML = renderers[state.currentStep - 1]() + renderColonnaOrdineDialog();
}

// `extra` is for actions outside the step flow — today just "Settings",
// placed in the page header, next to the comparison date. Grouped at the
// bottom next to "Continue" instead, it would be one of four actions in a
// row with nothing indicating which one is the actual next step. Settings
// isn't a step of the workflow and belongs where it's checked once.
function pageHeading(title, description, extra = "") {
  const run = state.review.run;
  const metadata = [run.label, run.createdAt ? formatDateTime(run.createdAt) : ""].filter(Boolean).join(" · ");
  return `
    <div class="page-heading">
      <div>
        <h2>${escapeHtml(title)}</h2>
        <p>${escapeHtml(description)}</p>
      </div>
      <div class="page-heading__meta">
        <span>${escapeHtml(metadata)}</span>
        ${extra}
      </div>
    </div>`;
}

// What to show for a document, without repeating it. The management-software
// export has `kind` and `supplier` set to the same value, and the card used
// to print that word twice in a row.
function etichetteDelDocumento(file) {
  const voci = [];
  for (const voce of [file.kind, file.supplier]) {
    const testo = String(voce || "").trim();
    if (!testo) continue;
    if (voci.some((gia) => gia.toLocaleLowerCase("it") === testo.toLocaleLowerCase("it"))) continue;
    voci.push(testo);
  }
  return voci;
}

function renderFileCard(file) {
  const status = fileStatus(file);
  const uploadName = String(file.uploadName || file.name || "");
  const gestionale = isManagementFile(file);
  const nomeDocumento = gestionale ? "elenco" : "listino";
  const confirming = file.deletable && state.uploadDeletion.confirming === uploadName;
  const deleting = state.uploadDeletion.deleting === uploadName;
  // Removing a price list during a recompute would change the result being
  // computed: the action stays visible but disabled, and says why.
  const bloccato = pipelineInCorso();
  // The document's message is the technical reason the pipeline matched a
  // given schema (e.g. "schema recognized from the registry (larice_v1,
  // confidence 0.97): no AI call needed"), identical across all documents
  // when everything went fine. That the file is fine is already shown by the
  // "ready" badge; this message is only useful when something's off, and
  // that's where it belongs. What matters about a document at all times —
  // which column the price comes from — is in the table below instead.
  // Also suppressed for documents the guided mapping below is currently
  // asking about: showing a generic "columns will be recognized on the next
  // comparison" message there would contradict what's actually happening —
  // it's asking for them right now, further down this same page.
  const inMappatura = documentiDellaFermata().has(String(file.name || "").toLocaleLowerCase("it"));
  const motivo = status.tone === "success" || inMappatura ? "" : String(file.message || "");
  return `
    <article class="file-card">
      <div class="file-card__top">
        <div class="file-card__name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
        ${renderBadge(status.label, status.tone)}
      </div>
      <div class="file-card__meta">
        ${etichetteDelDocumento(file).map((voce) => `<span>${escapeHtml(voce)}</span>`).join("")}
        ${file.rows ? `<span>${formatInteger(file.rows)} righe</span>` : ""}
      </div>
      ${motivo ? `<p class="file-card__message">${escapeHtml(motivo)}</p>` : ""}
      ${renderColonneDelDocumento(file)}
      ${file.deletable ? `
        <div class="file-card__actions">
          ${confirming ? `
            <span class="file-card__confirm">Tolgo «${escapeHtml(file.name)}» dal programma? Il file sul tuo computer resta dov’è. I prezzi in pagina restano quelli finché non rifai il confronto.</span>
            <button class="button button--danger-soft" type="button" data-action="confirm-delete-upload" data-upload-name="${escapeHtml(uploadName)}" ${deleting || bloccato ? "disabled" : ""}>${deleting ? "Eliminazione…" : "Elimina"}</button>
            <button class="button button--ghost" type="button" data-action="cancel-delete-upload" ${deleting ? "disabled" : ""}>Annulla</button>
          ` : `<button class="button button--ghost" type="button" data-action="ask-delete-upload" data-upload-name="${escapeHtml(uploadName)}" ${bloccato ? "disabled" : ""}>Elimina ${nomeDocumento}</button>`}
          ${bloccato ? '<span class="import-locked">Non ora: il confronto è in corso e toglierlo adesso cambierebbe il risultato.</span>' : ""}
        </div>` : ""}
    </article>`;
}

// --------------------------------------------------------------------------
// "Which columns it reads" — shown for every document, always
// --------------------------------------------------------------------------
// Before this table existed, the column mapping was only visible in one
// case: when the app failed to recognize a file and opened the guided
// mapping. For recognized documents — which is most of them, most weeks —
// the page just said "schema recognized from the registry" and nothing
// else; finding out which column the price came from meant opening the
// price list and counting columns by hand.
//
// Values come from the service already resolved (`GET /api/schemas/columns`):
// nothing is guessed here and no file is re-read. The column the app
// writes the order into is handled separately, since it's the only one
// that isn't read from the source.
function renderColonneDelDocumento(file) {
  const nome = String(file.name || "");
  const voce = state.colonneDocumenti.perNome[nome];
  const chiave = `colonne-documento:${nome}`;
  if (!voce) {
    if (state.colonneDocumenti.caricando || !state.colonneDocumenti.caricate) return "";
    // A document with no answer isn't hidden: "I don't know" is itself the
    // information, and staying silent would make it look like a forgotten
    // table instead.
    const motivo = state.colonneDocumenti.motivo || state.colonneDocumenti.errore;
    if (!motivo) return "";
    return `
      <details class="doc-columns" ${apribile(chiave)}>
        <summary>Quali colonne leggo</summary>
        <p class="doc-columns__note">${escapeHtml(motivo)}</p>
      </details>`;
  }
  const colonne = asArray(voce.columns);
  const confermata = String(voce.origin || "") === "confermata";
  const posizione = [
    voce.sheet ? `foglio «${voce.sheet}»` : "",
    voce.headerRow ? `intestazioni alla riga ${voce.headerRow}` : "nessuna riga di intestazioni",
    voce.dataStartRow ? `prodotti dalla riga ${voce.dataStartRow}` : "",
  ].filter(Boolean).join(" · ");
  return `
    <details class="doc-columns" ${apribile(chiave)}>
      <summary>Quali colonne leggo${colonne.length ? "" : " — non lo so"}</summary>
      <p class="doc-columns__note">${escapeHtml(posizione)}</p>
      ${colonne.length ? `
        <table class="doc-columns__table">
          <thead><tr><th>Col.</th><th>Intestazione</th><th>La leggo come</th><th>Esempio</th></tr></thead>
          <tbody>
            ${colonne.map((riga) => riga.trovata === false ? `
              <tr class="is-missing">
                <td>—</td>
                <td>${escapeHtml(String(riga.dichiarata || ""))}</td>
                <td>${escapeHtml(String(riga.etichetta || ""))}</td>
                <td>non trovata nel documento</td>
              </tr>` : `
              <tr>
                <td><strong>${escapeHtml(String(riga.lettera || ""))}</strong></td>
                <td>${escapeHtml(String(riga.intestazione || "—"))}</td>
                <td>${escapeHtml(String(riga.etichetta || ""))}</td>
                <td class="doc-columns__sample">${escapeHtml(String(riga.esempio || ""))}</td>
              </tr>`).join("")}
          </tbody>
        </table>` : '<p class="doc-columns__note">Di questo documento non so quali colonne leggere.</p>'}
      ${renderColonnaDellOrdine(file, voce)}
      ${renderOffertePerDocumento(file, voce)}
      <p class="doc-columns__origin">
        <span>${confermata ? "Assegnazione confermata da te." : "Assegnazione dichiarata dal registro dei fornitori."}</span>
        ${renderComandoRivediColonne(file)}
      </p>
    </details>`;
}

// Reviews ALL columns, not only the order column.
//
// The column selector has existed since day one, but only opens when the
// pipeline stalls on a schema the registry doesn't recognize — and with a
// recognized supplier it never stalls. The manual selector with a preview
// was effectively unreachable once a supplier's schema was learned: it
// hadn't disappeared, there was simply no path left to open it.
//
// Placed here, not next to "Delete", because this is the panel where the
// user is already looking at the columns: a wrong row gets corrected from
// where it was spotted.
function renderComandoRivediColonne(file) {
  const nome = String(file.name || "");
  if (!nome || mode === "demo") return "";
  const bloccato = pipelineInCorso();
  return `<button type="button" class="button button--ghost button--piccolo"
    data-action="apri-colonne" data-upload-name="${escapeHtml(nome)}"
    data-focus-key="colonne-documento-${escapeHtml(nome)}" ${bloccato ? "disabled" : ""}
    title="${bloccato ? "Non ora: il confronto è in corso." : "Correggi dove stanno prezzo, codice, descrizione e pezzi per collo"}">Rivedi le colonne</button>`;
}

// The column the app writes the order into, not one it reads — and the
// way to change it.
//
// The guided mapping that otherwise sets this column only opens when the
// app fails to recognize a document's columns, not for a supplier it
// already knows. For an already-known supplier, this button is the only
// way to change it without editing the registry's JSON file by hand.
//
// The button is hidden when the registry declares no order column at all:
// there's nothing to move in that case, only a write configuration still to
// be created, and that's created by the guided mapping along with everything
// else.
function renderColonnaDellOrdine(file, voce) {
  if (!voce.orderColumn) return "";
  const fornitore = String(file.supplierId || "").trim();
  const lettera = String(voce.orderColumn.lettera || "");
  const titolo = voce.orderColumn.intestazione ? ` («${escapeHtml(voce.orderColumn.intestazione)}»)` : "";
  return `
    <p class="doc-columns__note doc-columns__note--ordine">
      <span>L’ordine viene scritto nella colonna <strong>${escapeHtml(lettera)}</strong>${titolo}, sulla copia del listino.</span>
      ${fornitore && mode !== "demo" ? `<button type="button" class="button button--ghost button--piccolo"
        data-action="apri-colonna-ordine" data-supplier-id="${escapeHtml(fornitore)}"
        data-focus-key="colonna-ordine-${escapeHtml(fornitore)}">Cambia colonna</button>` : ""}
    </p>`;
}

// Where the supplier writes its promotional offers — the same kind of
// question as the other columns, hence shown here instead of in a separate
// panel.
//
// Covers a gap where a supplier's price list could be read and compiled
// while its offers silently weren't, with nothing on the page saying so.
// `commercialConditions: null` isn't a blank to hide: it's the difference
// between "this supplier makes no offers" and "it does, and we're not
// reading them", and the second one is something to act on.
function renderOffertePerDocumento(file, voce) {
  if (isManagementFile(file)) return "";
  const condizioni = voce.commercialConditions;
  const colonna = condizioni?.fields?.text;
  if (!colonna || !colonna.lettera) {
    return `<p class="doc-columns__note doc-columns__note--manca">Le offerte scritte su questo listino <strong>non le legge nessuno</strong>: manca la colonna in cui il fornitore le scrive. Indicamela quando ti chiedo le colonne di questo documento.</p>`;
  }
  return `<p class="doc-columns__note">Le offerte del fornitore si leggono nella colonna <strong>${escapeHtml(String(colonna.lettera))}</strong>.</p>`;
}

// The discards panel: how many rows from each price list didn't make it into
// the comparison, and why. Placed on the page where the user reviews their
// documents, since that's where "did the whole price list come through?" is
// actually asked. Numbers come from the run's audit trail; nothing here is
// computed.
// Reasons arrive from the service as machine labels (e.g. "senza_prezzo",
// "DISPLAY_COMPONENT") and are translated here into Italian for the user. A
// label not in this table is shown as-is: discard labels declared by the
// supplier registry are written by whoever configures that price list, and
// inventing a translation for them would hide the original.
const MOTIVI_DI_SCARTO = {
  senza_descrizione: "senza descrizione",
  senza_prezzo: "senza prezzo",
  senza_pezzi_per_collo: "senza pezzi per collo",
  non_disponibile: "non disponibili",
  senza_ean: "senza codice EAN",
  non_e_una_riga_prodotto: "non sono righe di prodotto",
  riga_fuori_dal_filtro: "fuori dal filtro del listino",
  motivo_non_dichiarato: "per un motivo che non ho dichiarato",
  OMAGGIO: "premi di una soglia con omaggio",
  DISPLAY_COMPONENT: "componenti di un espositore",
  BUNDLE_COMPONENT: "componenti di una confezione multipla",
};

function motivoDiScarto(label) {
  return MOTIVI_DI_SCARTO[label] || label;
}

function discardedRowsText(entry) {
  const pezzi = [];
  if (entry.notOrderableCount) {
    const dettaglio = entry.notOrderable.map((voce) => `${formatInteger(voce.count)} ${motivoDiScarto(voce.label)}`).join(", ");
    pezzi.push(`${formatInteger(entry.notOrderableCount)} non ordinabili (${dettaglio})`);
  }
  if (entry.excludedCount) {
    const dettaglio = entry.excluded.map((voce) => `${formatInteger(voce.count)} ${motivoDiScarto(voce.label)}`).join(", ");
    pezzi.push(`${formatInteger(entry.excludedCount)} escluse dal filtro (${dettaglio})`);
  }
  const scartate = entry.notOrderableCount + entry.excludedCount;
  if (!scartate) return "";
  return `${formatInteger(scartate)} ${scartate === 1 ? "riga scartata" : "righe scartate"} — ${pezzi.join(", ")}`;
}

function renderDiscardedRowsPanel() {
  const righe = asArray(state.review?.discardedRows);
  if (!righe.length) return "";
  const totale = righe.reduce((sum, entry) => sum + entry.notOrderableCount + entry.excludedCount, 0);
  const doppioniTotali = righe.reduce((sum, entry) => sum + entry.duplicateEans, 0);
  // The badge reports whichever count is actually nonzero: showing "0 rows"
  // under a "discards" title when the panel only contains duplicate
  // barcodes would say two contradictory things at once.
  const badge = totale
    ? renderBadge(`${formatInteger(totale)} ${totale === 1 ? "riga scartata" : "righe scartate"}`, "warning")
    : renderBadge(`${formatInteger(doppioniTotali)} ${doppioniTotali === 1 ? "codice ripetuto" : "codici ripetuti"}`, "neutral");
  return `
    <details class="panel import-panel audit-disclosure" ${apribile("righe-scartate")}>
      <summary class="audit-disclosure__summary">
        <span class="audit-disclosure__title">Righe di listino rimaste fuori</span>
        ${badge}
      </summary>
      <div class="audit-disclosure__body">
        <p>Quali righe non sono entrate nel confronto e perché.</p>
        <ul class="discarded-rows">
          ${righe.map((entry) => {
            const scarti = discardedRowsText(entry);
            const doppioni = entry.duplicateEans
              ? `<span class="discarded-rows__duplicates">${formatInteger(entry.duplicateEans)} ${entry.duplicateEans === 1 ? "codice a barre ripetuto" : "codici a barre ripetuti"} nel listino: articoli diversi con lo stesso codice restano righe diverse.</span>`
              : "";
            // Says "rows read", not "rows included in the comparison":
            // discarded rows are already INSIDE that count, so showing both
            // numbers side by side would look additive when it isn't.
            return `
              <li>
                <strong>${escapeHtml(entry.supplierName)}</strong>
                ${scarti ? `<span>${escapeHtml(scarti)}${entry.rowsKept ? ` su ${formatInteger(entry.rowsKept)} righe lette` : ""}</span>` : ""}
                ${!scarti && entry.rowsKept ? `<span class="discarded-rows__kept">${formatInteger(entry.rowsKept)} righe lette</span>` : ""}
                ${doppioni}
              </li>`;
          }).join("")}
        </ul>
      </div>
    </details>`;
}

// Documents that were rejected, with name and reason. Has its own removal
// action: `remove-file` passes an index into `state.pendingFiles`, and
// reusing it here would remove a valid document instead of a rejected one.
function renderFileScartati(role) {
  const scartati = state.fileScartati
    .map((voce, indice) => ({ ...voce, indice }))
    .filter((voce) => voce.role === role);
  if (!scartati.length) return "";
  return `
    <ul class="file-scartati" aria-label="Documenti rimasti fuori">
      ${scartati.map(({ nome, motivo, indice }) => `
        <li>
          <span><strong>${escapeHtml(nome)}</strong> — ${escapeHtml(motivo)}</span>
          <button class="button button--ghost button--piccolo" type="button" data-action="togli-scartato" data-scartato-indice="${indice}">Togli l’avviso</button>
        </li>`).join("")}
    </ul>`;
}

function renderPendingFiles(role) {
  const selected = state.pendingFiles
    .map((entry, index) => ({ ...entry, index }))
    .filter((entry) => entry.role === role);
  if (!selected.length) return renderFileScartati(role);
  return `
    ${renderFileScartati(role)}
    <ul class="pending-files" aria-label="Documenti selezionati">
      ${selected.map(({ file, index }) => `
        <li>
          <span>${escapeHtml(file.name)}</span>
          <span>${formatBytes(file.size)} · <button class="button button--ghost" type="button" data-action="remove-file" data-file-index="${index}">Rimuovi</button></span>
        </li>`).join("")}
    </ul>
    <div class="button-row" style="margin-top: .8rem">
      <button class="button button--primary" type="button" data-action="upload" data-upload-role="${escapeHtml(role)}" ${state.uploading || pipelineInCorso() ? "disabled" : ""}>
        ${state.uploading ? "Caricamento e controllo…" : selected.length === 1 ? "Carica il documento scelto" : `Carica i ${selected.length} documenti scelti`}
      </button>
    </div>
    ${pipelineInCorso() ? '<p class="import-locked">Il confronto è in corso: i documenti si caricano appena ha finito, altrimenti cambierebbero il risultato mentre lo si sta calcolando.</p>' : ""}`;
}

// The recompute panel: one button, then the phases as they progress. It
// decides nothing on its own — state, phases, numbers and warnings all come
// from the local service, the only side that knows where the pipeline is.
const TONO_FASE = {
  COMPLETATO: "success",
  IN_CORSO: "info",
  ERRORE: "danger",
  IN_ATTESA: "neutral",
};

function pipelineInCorso() {
  return String(state.pipeline.stato?.stato || "") === "IN_CORSO";
}

// `IN_ATTESA` has two meanings. Without `cambiamento`, nothing has run yet: a
// bar at zero would be noise, so nothing is drawn. With `cambiamento`, the
// documents changed after the last comparison: the prices on pages 2 and 3
// are stale, and that needs to be said. The local service owns this state, so
// the banner survives a reload and disappears on its own once the comparison
// is redone.
//
// The field may be absent (service older than the page): fall back to
// showing nothing.
function cambiamentoDocumenti(stato = state.pipeline.stato) {
  const cambiamento = stato?.cambiamento;
  if (!cambiamento || typeof cambiamento !== "object") return null;
  const tipo = String(cambiamento.tipo || "");
  if (tipo !== "eliminato" && tipo !== "caricato" && tipo !== "colonne") return null;
  const nomi = (elenco) => asArray(elenco).map((voce) => String(voce ?? "").trim()).filter(Boolean);
  return { tipo, documenti: nomi(cambiamento.documenti), fornitori: nomi(cambiamento.fornitori) };
}

// The date of the comparison still on screen: it's what makes "the prices
// are stale" concrete.
function dataUltimoConfronto() {
  return formatWeekdayDayMonth(state.review?.run?.createdAt);
}

function elencoNomi(nomi) {
  if (!nomi.length) return "";
  if (nomi.length === 1) return nomi[0];
  return `${nomi.slice(0, -1).join(", ")} e ${nomi[nomi.length - 1]}`;
}

// The banner's two sentences: what changed, and what you're looking at now.
// Suppliers are named when known — "CIPRESSO" says more than the file name —
// and file names otherwise.
function frasiCambiamentoDocumenti(cambiamento = cambiamentoDocumenti()) {
  if (!cambiamento) return null;
  const perNome = cambiamento.fornitori.length ? cambiamento.fornitori : cambiamento.documenti;
  const uno = perNome.length <= 1;
  // "You uploaded" would be wrong for a manually-corrected column mapping:
  // the document is the same, only how it's read has changed.
  const FRASI = {
    eliminato: { verbo: "Hai eliminato", uno: "il listino", molti: "i listini" },
    caricato: { verbo: "Hai caricato", uno: "il listino", molti: "i listini" },
    colonne: { verbo: "Hai cambiato le colonne", uno: "del listino", molti: "dei listini" },
  };
  const frase = FRASI[cambiamento.tipo] || FRASI.caricato;
  const titolo = perNome.length
    ? `${frase.verbo} ${uno ? frase.uno : frase.molti} ${elencoNomi(perNome)} dopo l’ultimo confronto.`
    : `${frase.verbo} dei documenti dopo l’ultimo confronto.`;
  const dove = state.currentStep === 1 ? "nelle pagine 2 e 3" : "qui sotto";
  const data = dataUltimoConfronto();
  const prezzi = data
    ? `I prezzi che vedi ${dove} sono ancora quelli di ${data}.`
    : `I prezzi che vedi ${dove} sono ancora quelli dell’ultimo confronto.`;
  return { titolo, prezzi };
}

// The banner blocks nothing: it states what changed and lets work continue.
// Outside page 1 it also carries a link back to the recompute button.
function renderCambiamentoDocumenti() {
  const frasi = frasiCambiamentoDocumenti();
  if (!frasi) return "";
  const altrove = state.currentStep !== 1;
  return `
    <div class="stale-comparison" role="status">
      <span class="stale-comparison__icon" aria-hidden="true">!</span>
      <div class="stale-comparison__body">
        <strong>${escapeHtml(frasi.titolo)}</strong>
        <p>${escapeHtml(frasi.prezzi)}</p>
      </div>
      ${altrove ? '<button class="button button--secondary" type="button" data-action="vai-al-ricalcolo">Vai a rifare il confronto</button>' : ""}
    </div>`;
}

// --------------------------------------------------------------------------
// One primary button at a time (page 1)
// --------------------------------------------------------------------------
// Document state picks which action is primary; the others stay visible as
// secondary. Nothing is taken out of the user's hands except when it's truly
// impossible at that moment.

function documentiCaricati() {
  return asArray(state.review?.files).length > 0;
}

// "A comparison exists" means there's something to look at on pages 2 and 3.
// `run.status` is "ready" even on first launch with zero documents, so it
// can't distinguish "ready" from "not done yet"; checking the product count
// avoids that false positive.
function confrontoDisponibile() {
  return asArray(state.review?.products).length > 0;
}

// What's missing before a comparison is possible: names the missing document
// instead of just disabling the button.
function documentiMancantiText() {
  const files = asArray(state.review?.files);
  const elenco = files.some(isManagementFile);
  const listini = files.some((file) => !isManagementFile(file));
  if (elenco && listini) return "";
  if (!elenco && !listini) return "Carica prima l’elenco dei prodotti e almeno un listino.";
  if (!elenco) return "Manca l’elenco dei prodotti da ordinare: caricalo qui sopra.";
  return "Manca almeno un listino fornitore: caricalo qui sopra.";
}

// The eight states of page 1, most specific first.
function statoPaginaImporta() {
  // A running recompute takes priority over everything, including files
  // already picked but not uploaded: uploads are locked while the pipeline
  // runs, so an active "upload" prompt would be a lie.
  if (pipelineInCorso()) return "RICALCOLO_IN_CORSO";
  if (state.pendingFiles.length) return "FILE_SCELTI";
  if (schemaMappingRequired()) return "COLONNE_SCONOSCIUTE";
  if (String(state.pipeline.stato?.stato || "") === "ERRORE") return "RICALCOLO_FALLITO";
  if (cambiamentoDocumenti()) return "DOCUMENTI_CAMBIATI";
  if (documentiMancantiText()) return "NIENTE_CARICATO";
  if (confrontoDisponibile()) return "CONFRONTO_AGGIORNATO";
  return "PRONTI_MAI_CONFRONTATI";
}

// The recompute button. "Recompute" on the first run would be a lie — there's
// nothing to recompute yet — so the label depends on state instead of being fixed.
function comandoConfronto(situazione = statoPaginaImporta()) {
  if (state.pipeline.avviando) {
    return { etichetta: "Avvio del confronto…", tono: "secondary", disabilitato: true, nota: "" };
  }
  if (situazione === "RICALCOLO_IN_CORSO") {
    // No primary action while the pipeline runs: the progress bar and phases
    // already say where it is.
    return { etichetta: "Confronto in corso…", tono: "secondary", disabilitato: true, nota: "" };
  }
  if (situazione === "NIENTE_CARICATO") {
    return {
      etichetta: "Confronta i listini",
      tono: "primary",
      disabilitato: true,
      nota: documentiMancantiText(),
    };
  }
  if (situazione === "RICALCOLO_FALLITO") {
    return { etichetta: "Riprova il confronto", tono: "primary", disabilitato: false, nota: "" };
  }
  if (situazione === "DOCUMENTI_CAMBIATI") {
    return { etichetta: "Aggiorna il confronto", tono: "primary", disabilitato: false, nota: "" };
  }
  if (situazione === "FILE_SCELTI") {
    // Disabled, not just secondary: picking new files and forgetting to press
    // "Upload" would silently run the comparison against last week's price
    // lists. Removing the picked files from the list above re-enables the
    // command.
    return {
      etichetta: confrontoDisponibile() ? "Rifai il confronto" : "Confronta i listini",
      tono: "secondary",
      disabilitato: true,
      nota: "Hai dei documenti scelti e non ancora caricati: caricali con il comando qui sopra, oppure toglili dall’elenco. Altrimenti il confronto non li vedrebbe.",
    };
  }
  if (situazione === "CONFRONTO_AGGIORNATO") {
    return { etichetta: "Rifai il confronto", tono: "secondary", disabilitato: false, nota: "" };
  }
  if (situazione === "COLONNE_SCONOSCIUTE") {
    return { etichetta: "Confronta i listini", tono: "secondary", disabilitato: true, nota: "" };
  }
  return { etichetta: "Confronta i listini", tono: "primary", disabilitato: false, nota: "" };
}

// The "Continue" button. Must never lie: when the comparison is older than
// the documents it stays clickable — blocking the work would be worse — but
// says which comparison it leads to.

// How many products in the comparison come from the management-software
// export. Must match the row count shown on that document's card, so a
// correct upload is visible without opening anything. The full product count
// also includes price-list displays and manually added items, which would
// make a correct document look wrong. The full breakdown is at the top of
// page 2.
function prodottiDelGestionale() {
  return asArray(state.review?.products).filter(
    (prodotto) => !prodotto.addedManually && prodotto.itemType !== "display",
  ).length;
}

function comandoContinua(situazione = statoPaginaImporta()) {
  const quanti = asArray(state.review?.products).length;
  if (!quanti) {
    return {
      etichetta: "Continua",
      tono: "secondary",
      disabilitato: true,
      nota: "Prima carica i documenti e premi «Confronta i listini».",
    };
  }
  const data = dataUltimoConfronto();
  if (situazione === "DOCUMENTI_CAMBIATI") {
    return {
      etichetta: data ? `Continua sul confronto di ${data}` : "Continua sul confronto precedente",
      tono: "secondary",
      disabilitato: false,
      nota: "",
    };
  }
  if (situazione === "RICALCOLO_IN_CORSO") {
    return {
      etichetta: data ? `Vai al confronto di ${data} →` : "Vai al confronto precedente →",
      tono: "secondary",
      disabilitato: false,
      nota: "",
    };
  }
  const dalGestionale = prodottiDelGestionale();
  const etichetta = `Continua con ${formatInteger(dalGestionale)} ${dalGestionale === 1 ? "prodotto" : "prodotti"} del gestionale →`;
  const primario = situazione === "CONFRONTO_AGGIORNATO";
  return { etichetta, tono: primario ? "primary" : "secondary", disabilitato: false, nota: "" };
}

function renderNumeriPipeline(numeri) {
  const voci = [];
  if (numeri.documenti) voci.push(`${formatInteger(numeri.documenti)} documenti`);
  if (numeri.fornitori) voci.push(`${formatInteger(numeri.fornitori)} fornitori`);
  // Two numbers, not one: the total product count is the comparison's size,
  // the management-file count is what must match the document card's row
  // count. Showing only the total made it look like a different list had
  // been read than the one uploaded.
  if (numeri.prodotti) {
    const dalGestionale = Number(numeri.prodottiGestionale) || 0;
    voci.push(dalGestionale && dalGestionale !== Number(numeri.prodotti)
      ? `${formatInteger(numeri.prodotti)} prodotti, di cui ${formatInteger(dalGestionale)} dal gestionale`
      : `${formatInteger(numeri.prodotti)} prodotti`);
  }
  // "Cases to review" and "decided" already live, with more context, inside
  // the pipeline phases; repeating them here would just add noise to a line
  // read at a glance.
  if (numeri.spesaUsd) voci.push(`${Number(numeri.spesaUsd).toFixed(3)} $`);
  if (!voci.length) return "";
  return `<p class="file-card__meta">${voci.map((voce) => `<span>${escapeHtml(voce)}</span>`).join("")}</p>`;
}

function schemaMappingRequired(stato = state.pipeline.stato) {
  return String(stato?.fermata?.code || "") === "SCHEMA_SCONOSCIUTO";
}

// The documents the schema-mapping stop is currently asking to configure.
function documentiDellaFermata(stato = state.pipeline.stato) {
  if (!schemaMappingRequired(stato)) return new Set();
  return new Set(
    asArray(stato?.fermata?.documenti).map((nome) => String(nome || "").toLocaleLowerCase("it")),
  );
}

function schemaDocumentValue(documento) {
  return state.schemaMapping.values[String(documento.profileId || "")] || {};
}

function schemaSelectedSheet(documento, valore = schemaDocumentValue(documento)) {
  return (documento.sheets || []).find((foglio) => String(foglio.name || "") === String(valore.sheet || ""))
    || (documento.sheets || [])[0]
    || { name: "", rows: [], columns: [], headerRows: [], maxColumn: 1 };
}

function schemaHeaderValues(foglio, headerRow) {
  const riga = (foglio.rows || []).find((voce) => Number(voce.row) === Number(headerRow));
  return Array.isArray(riga?.values) ? riga.values : [];
}

function schemaColumnLabel(foglio, valore, indice) {
  const lettera = schemaColumnLetter(indice);
  const intestazione = schemaHeaderValues(foglio, valore.headerRow)[indice - 1];
  if (String(intestazione ?? "").trim()) return `${lettera} — ${String(intestazione).trim()}`;
  const profilo = (foglio.columns || []).find((voce) => Number(voce.index) === indice);
  const esempio = (profilo?.examples || []).find((voce) => String(voce ?? "").trim());
  return esempio === undefined ? `${lettera} — colonna vuota` : `${lettera} — es. ${String(esempio).trim()}`;
}

function schemaColumnLetter(indice) {
  let numero = Math.max(1, Math.trunc(Number(indice) || 1));
  let risultato = "";
  while (numero > 0) {
    numero -= 1;
    risultato = String.fromCharCode(65 + (numero % 26)) + risultato;
    numero = Math.floor(numero / 26);
  }
  return risultato;
}

// A wizard field's id, built from the document and field it belongs to. Feeds
// the label's `for` attribute — without it, screen readers get unnamed
// dropdowns and clicking the label doesn't focus the field. The `c-` prefix
// isn't decoration: `profileId` can start with a digit, which is valid as an
// HTML5 id but breaks unescaped CSS selectors.
function idCampoSchema(profileId, campo) {
  return `c-${profileId}-${campo}`;
}

function renderSchemaColumnSelect(documento, campo, etichetta, { optional = false, order = false } = {}) {
  const valore = schemaDocumentValue(documento);
  const foglio = schemaSelectedSheet(documento, valore);
  const corrente = order ? valore.orderColumn : valore.columns?.[campo];
  const massimo = Math.max(1, Number(foglio.maxColumn || 1) + (order ? 1 : 0));
  const opzioni = [];
  if (optional) opzioni.push(`<option value="" ${corrente ? "" : "selected"}>Non presente</option>`);
  else opzioni.push(`<option value="" ${corrente ? "" : "selected"}>Scegli la colonna</option>`);
  for (let indice = 1; indice <= massimo; indice += 1) {
    opzioni.push(`<option value="${indice}" ${Number(corrente) === indice ? "selected" : ""}>${escapeHtml(schemaColumnLabel(foglio, valore, indice))}</option>`);
  }
  const identificativo = escapeHtml(idCampoSchema(documento.profileId, order ? "orderColumn" : campo));
  return `
    <div class="field">
      <label for="${identificativo}">${escapeHtml(etichetta)}</label>
      <select id="${identificativo}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" ${order ? 'data-schema-field="orderColumn"' : `data-schema-column="${escapeHtml(campo)}"`}>
        ${opzioni.join("")}
      </select>
    </div>`;
}

// The section breaks the profile found at the top of the document, and the
// one the user picked. A fixed row number is fragile: on some suppliers the
// product list starts after a marker cell that moves from file to file
// (e.g. free-goods valuation rows before it), so a hardcoded row would
// silently point at the wrong place the next week.
function schemaSectionBreaks(foglio) {
  return (foglio.sectionBreaks || []).filter((voce) => Number(voce.row) > 0);
}

function schemaSelectedBreak(foglio, valore) {
  if (!valore.dataStartBreak) return null;
  return schemaSectionBreaks(foglio).find((voce) => Number(voce.row) === Number(valore.dataStartBreak)) || null;
}

// Picking a section break also derives the start row, so it stops being
// something to type by hand. Lives here rather than in the event handler so
// it's reachable from tests — this is exactly the part that's easy to get
// wrong.
function schemaApplicaSeparatore(valore, foglio) {
  const separatore = schemaSelectedBreak(foglio, valore);
  if (!separatore) {
    valore.markerText = "";
    return valore;
  }
  valore.dataStartRow = Number(separatore.data_from || Number(separatore.row) + 1);
  valore.markerText = schemaTestoSeparatore(separatore);
  return valore;
}

// The section break text without the ellipsis the profile uses to shorten it:
// this is what can actually be searched for in the cell, and what the user
// can shorten further.
function schemaTestoSeparatore(separatore) {
  const testo = String(separatore.text ?? "").trim();
  return testo.endsWith("…") ? testo.slice(0, -1).trim() : testo;
}

// The rule the page sends to the service instead of a raw row number.
// `offset` is derived from the two rows the user sees — the break and the
// first product — so it can't describe a row other than the one shown in the
// preview.
function schemaDataStartMarker(foglio, valore) {
  const separatore = schemaSelectedBreak(foglio, valore);
  if (!separatore) return undefined;
  // The profile truncates long break text to 80 characters with an ellipsis;
  // many real section markers are long promotional lines. Asking the service
  // for an exact match on truncated text would fail, since it compares
  // against the full cell.
  const mozzato = String(separatore.text ?? "").trim().endsWith("…");
  const intero = schemaTestoSeparatore(separatore);
  const testo = String(valore.markerText ?? intero).trim();
  if (!testo) return undefined;
  const scarto = Number(valore.dataStartRow) - Number(separatore.row);
  if (!Number.isFinite(scarto) || scarto < 0) return undefined;
  return {
    column: Number(separatore.column),
    // A truncated text — whether by the profile or the user — is a fragment
    // of a longer string, often the stable part of one that changes weekly
    // (a date range in a promo header): there, "contains" is the right match.
    match: !mozzato && testo === intero ? "equals" : "contains",
    text: testo,
    offset: scarto,
  };
}

function renderSchemaDataStart(documento) {
  const valore = schemaDocumentValue(documento);
  const foglio = schemaSelectedSheet(documento, valore);
  const separatori = schemaSectionBreaks(foglio);
  const scelto = schemaSelectedBreak(foglio, valore);
  const massimo = Number(foglio.maxRow || 1);
  const numero = `
    <div class="field">
      <label for="${escapeHtml(idCampoSchema(documento.profileId, "dataStartRow"))}">Prima riga dei prodotti</label>
      <input id="${escapeHtml(idCampoSchema(documento.profileId, "dataStartRow"))}" class="input" type="number" min="1" max="${massimo}" value="${Number(valore.dataStartRow || 2)}" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="dataStartRow" ${scelto ? "readonly" : ""}>
    </div>`;
  if (!separatori.length) return numero;
  const opzioni = [`<option value="" ${scelto ? "" : "selected"}>La riga che scrivo qui accanto</option>`]
    .concat(separatori.map((voce) => `<option value="${Number(voce.row)}" ${scelto && Number(scelto.row) === Number(voce.row) ? "selected" : ""}>Dopo la riga ${Number(voce.row)} («${escapeHtml(String(voce.text ?? ""))}»)</option>`));
  return `
    <div class="field">
      <label for="${escapeHtml(idCampoSchema(documento.profileId, "dataStartBreak"))}">I prodotti cominciano</label>
      <select id="${escapeHtml(idCampoSchema(documento.profileId, "dataStartBreak"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="dataStartBreak">
        ${opzioni.join("")}
      </select>
    </div>
    ${scelto ? `<div class="field"><label for="${escapeHtml(idCampoSchema(documento.profileId, "markerText"))}">Testo che separa i prodotti</label><input id="${escapeHtml(idCampoSchema(documento.profileId, "markerText"))}" class="input" value="${escapeHtml(String(valore.markerText ?? schemaTestoSeparatore(scelto)))}" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="markerText" autocomplete="off"></div>` : ""}
    ${numero}`;
}

function renderSchemaPreview(documento) {
  const valore = schemaDocumentValue(documento);
  const foglio = schemaSelectedSheet(documento, valore);
  const separatore = schemaSelectedBreak(foglio, valore);
  const da = Math.max(1, Number(valore.headerRow || 1) - 1);
  const a = Number(valore.dataStartRow || Number(valore.headerRow || 1) + 1) + 7;
  let righe = (foglio.rows || []).filter((voce) => Number(voce.row) >= da && Number(voce.row) <= a);
  if (!righe.length) righe = (foglio.rows || []).slice(0, 10);
  const selezionate = new Set(Object.values(valore.columns || {}).map(Number).filter(Boolean));
  if (Number(valore.orderColumn)) selezionate.add(Number(valore.orderColumn));
  const massimoVisibile = Math.min(30, Math.max(Number(foglio.maxColumn || 1), ...selezionate));
  const colonne = Array.from({ length: massimoVisibile }, (_voce, indice) => indice + 1);
  return `
    <div class="schema-preview" role="region" aria-label="Anteprima di ${escapeHtml(documento.fileName)}" tabindex="0">
      <table>
        <thead><tr><th>Riga</th>${colonne.map((indice) => `<th class="${selezionate.has(indice) ? "is-mapped" : ""}">${schemaColumnLetter(indice)}</th>`).join("")}</tr></thead>
        <tbody>
          ${righe.map((riga) => `
            <tr class="${Number(riga.row) === Number(valore.headerRow) ? "is-header" : ""}${separatore && Number(riga.row) === Number(separatore.row) ? " is-break" : ""}">
              <th>${formatInteger(riga.row)}</th>
              ${colonne.map((indice) => `<td class="${selezionate.has(indice) ? "is-mapped" : ""}" title="${escapeHtml(riga.values?.[indice - 1] ?? "")}">${escapeHtml(riga.values?.[indice - 1] ?? "")}</td>`).join("")}
            </tr>`).join("")}
        </tbody>
      </table>
    </div>`;
}

function renderSchemaValidation(documento) {
  const risultato = state.schemaMapping.results[String(documento.profileId || "")];
  if (!risultato) return "";
  const campione = Array.isArray(risultato.sample) ? risultato.sample.slice(0, 3) : [];
  return `
    <div class="schema-validation">
      <strong>${formatInteger(risultato.rowsUsable)} righe utilizzabili</strong>
      <span>su ${formatInteger(risultato.rowsRead)} lette</span>
      ${campione.length ? `<ul>${campione.map((voce) => `<li>${escapeHtml(voce.description || voce.ean || "Riga letta")} · ${escapeHtml(voce.piecesPerCarton || "")} ${voce.price !== undefined ? `· ${formatEuro(voce.price)}` : ""}</li>`).join("")}</ul>` : ""}
    </div>`;
}

// Two different situations, kept distinct instead of sharing one message.
// "I don't recognize this" asks to configure a new supplier. "I recognize
// this and its layout changed" only asks to double-check: the dropdowns are
// already filled with what the adapter registry declares. The second case is
// common when only the sheet name (which carries a date) or the header row
// shifts week to week, while the columns stay in place.
function renderPercheEQui(documento) {
  const motivo = documento.reason || {};
  if (String(motivo.state || "") === "RUOLO_SBAGLIATO") {
    const chi = String(motivo.supplierName || "").trim();
    return `<p>${escapeHtml(chi
      ? `Questo documento lo riconosco: è il listino di ${chi}, ed è stato caricato con il tipo sbagliato. Cambia «Tipo di documento» qui sotto.`
      : "Questo documento lo riconosco, ma è stato caricato con il tipo sbagliato. Cambia «Tipo di documento» qui sotto.")}</p>`;
  }
  // "I don't recognize this" was also shown for a document missing one
  // header out of five, with all the others in place — e.g. a price list
  // re-saved from Excel with one header cell emptied by accident, which used
  // to get misconfigured as a brand-new supplier. The adapter registry is the
  // only side that knows which cell is missing and where it stood, so it
  // decides the wording here.
  if (String(motivo.state || "") === "QUASI") {
    const chi = String(motivo.supplierName || "").trim();
    const mancanti = asArray(motivo.missing)
      .map((voce) => {
        const titolo = String(voce?.header || "").trim();
        const colonna = String(voce?.column || "").trim();
        if (!titolo) return "";
        return colonna ? `«${titolo}» nella colonna ${colonna}` : `«${titolo}»`;
      })
      .filter(Boolean);
    if (chi && mancanti.length) {
      const quante = Number(motivo.present || 0);
      const manca = mancanti.length === 1 ? "manca l’intestazione" : "mancano le intestazioni";
      const altre = quante > 1
        ? `Le altre ${quante} obbligatorie ci sono tutte, e tutte al posto giusto.`
        : "";
      const celle = mancanti.length === 1
        ? "quella cella può essere stata svuotata"
        : "quelle celle possono essere state svuotate";
      return `<p>${escapeHtml(
        `Sembra il listino di ${chi}, ma ${manca} ${mancanti.join(" e ")}. ${altre} ` +
        `Se il file è stato aperto in Excel e risalvato, ${celle} senza volerlo: la cosa ` +
        `giusta da fare è ricaricare l’originale del fornitore, non configurarlo qui come ` +
        `se fosse nuovo.`
      )}</p>`;
    }
  }
  // Manually reviewing the columns of a recognized price list: nothing is
  // wrong here, and saying so matters — the user needs to know they're
  // looking at the right document before changing its columns.
  if (String(motivo.state || "") === "NOTO") {
    const chi = String(motivo.supplierName || "").trim();
    return `<p>${escapeHtml(chi
      ? `Questo listino è di ${chi} e lo riconosco: le colonne qui sotto sono quelle che uso oggi.`
      : "Questo documento lo riconosco: le colonne qui sotto sono quelle che uso oggi.")}</p>`;
  }
  if (String(motivo.state || "") !== "VARIATO") return "";
  const fornitore = String(motivo.supplierName || "").trim();
  const cambiato = asArray(motivo.changed).map((voce) => String(voce || "")).filter(Boolean);
  // If nothing pre-filled the dropdowns, "these are already the columns I
  // used" would be false. That happens for suppliers recognized from column
  // shape rather than headers, which have nothing to propose.
  const compilate = Boolean(schemaDocumentValue(documento).supplierChoice);
  const chi = fornitore ? `Questo listino è di ${fornitore} e lo conosco` : "Questo listino lo conosco";
  // "What changed:" rather than a conjugated sentence: the listed items have
  // different grammatical gender and number in Italian, so any single verb
  // agreement would be wrong for at least one of them.
  const cosa = cambiato.length ? cambiato.join(", ") : "qualcosa nella sua disposizione";
  const coda = compilate
    ? "Le colonne qui sotto sono già quelle che usavo: controllale e conferma."
    : "Le colonne di questo fornitore non si leggono dalle intestazioni, quindi qui sotto vanno indicate.";
  return `<p>${escapeHtml(`${chi}. Quello che è cambiato: ${cosa}. ${coda}`)}</p>`;
}

// A conflict with an already-loaded price list, surfaced while choosing, not
// after confirming. The rule matches the pipeline's own (`_piu_recente_per_ruolo`):
// the file copied to the uploads folder most recently wins. On a tie, no
// guess is made — the page says it can't tell.
function schemaOccupatoDa(valore) {
  const occupati = state.schemaMapping.data?.occupied || [];
  if (valore.role === "master") return occupati.find((voce) => voce.role === "master") || null;
  if (!valore.supplierChoice || valore.supplierChoice === "__new__") return null;
  return occupati.find((voce) => voce.role === "supplier" && voce.supplierId === valore.supplierChoice) || null;
}

function renderSostituzione(documento, valore) {
  const occupato = schemaOccupatoDa(valore);
  if (!occupato) return "";
  const mio = String(documento.modifiedAt || "");
  const suo = String(occupato.modifiedAt || "");
  const esito = mio > suo
    ? `resterebbe fuori «${occupato.fileName}»`
    : mio < suo
      ? `resterebbe fuori proprio «${documento.fileName}», cioè questo`
      : "i due file risultano copiati nello stesso momento, e non so dire quale dei due resta fuori";
  return renderAlert({
    title: "C’è già un listino di questo fornitore",
    message: `${occupato.supplierName} ha già «${occupato.fileName}» in questo confronto, e nel confronto ne entra uno solo: ${esito}. Se non è quello che vuoi, togli l’altro documento dal passo 1 oppure scegli «Nuovo fornitore…».`,
    severity: "warning",
  });
}

function renderSchemaDocument(documento) {
  const valore = schemaDocumentValue(documento);
  const foglio = schemaSelectedSheet(documento, valore);
  const ruoloFornitore = valore.role !== "master";
  const nuovo = valore.supplierChoice === "__new__";
  const fattoreFisso = valore.factorMode === "fixed";
  return `
    <article class="schema-document">
      <div class="schema-document__head">
        <div>
          <h4>${escapeHtml(documento.fileName)}</h4>
          <p>${escapeHtml(String(documento.format || "").toUpperCase())} · ${formatInteger(foglio.maxRow || 0)} righe</p>
          ${renderPercheEQui(documento)}
        </div>
        ${state.schemaMapping.results[documento.profileId] ? renderBadge("Controllato", "success") : renderBadge("Da controllare", "warning")}
      </div>
      <div class="schema-routing">
        <div class="field">
          <label for="${escapeHtml(idCampoSchema(documento.profileId, "role"))}">Tipo di documento</label>
          <select id="${escapeHtml(idCampoSchema(documento.profileId, "role"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="role">
            <option value="supplier" ${ruoloFornitore ? "selected" : ""}>Listino fornitore</option>
            <option value="master" ${ruoloFornitore ? "" : "selected"}>Elenco del gestionale</option>
          </select>
        </div>
        ${ruoloFornitore ? `
          <div class="field">
            <label for="${escapeHtml(idCampoSchema(documento.profileId, "supplierChoice"))}">Fornitore</label>
            <select id="${escapeHtml(idCampoSchema(documento.profileId, "supplierChoice"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="supplierChoice">
              <option value="" ${valore.supplierChoice ? "" : "selected"}>Scegli il fornitore…</option>
              ${(state.schemaMapping.data?.suppliers || []).map((fornitore) => `<option value="${escapeHtml(fornitore.id)}" ${valore.supplierChoice === fornitore.id ? "selected" : ""}>${escapeHtml(fornitore.name)}</option>`).join("")}
              <option value="__new__" ${nuovo ? "selected" : ""}>Nuovo fornitore…</option>
            </select>
          </div>
          ${nuovo ? `<div class="field"><label for="${escapeHtml(idCampoSchema(documento.profileId, "supplierName"))}">Nome del nuovo fornitore</label><input id="${escapeHtml(idCampoSchema(documento.profileId, "supplierName"))}" class="input" value="${escapeHtml(valore.supplierName || "")}" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="supplierName" autocomplete="off"></div>` : ""}
        ` : ""}
        ${/* Outside the supplier-only branch: `schemaOccupatoDa` also covers a
             conflict on the management file, which `ruoli_gia_occupati` reports
             for too. */ ""}
        ${renderSostituzione(documento, valore)}
        <div class="field">
          <label for="${escapeHtml(idCampoSchema(documento.profileId, "sheet"))}">Foglio</label>
          <select id="${escapeHtml(idCampoSchema(documento.profileId, "sheet"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="sheet">
            ${(documento.sheets || []).map((voce) => `<option value="${escapeHtml(voce.name || "")}" ${String(valore.sheet || "") === String(voce.name || "") ? "selected" : ""}>${escapeHtml(voce.name || "CSV")}</option>`).join("")}
          </select>
        </div>
        <div class="field"><label for="${escapeHtml(idCampoSchema(documento.profileId, "headerRow"))}">Riga delle intestazioni</label><input id="${escapeHtml(idCampoSchema(documento.profileId, "headerRow"))}" class="input" type="number" min="1" max="${Number(foglio.maxRow || 1)}" value="${Number(valore.headerRow || 1)}" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="headerRow"></div>
        ${renderSchemaDataStart(documento)}
      </div>
      ${renderSchemaPreview(documento)}
      <div class="schema-fields">
        ${renderSchemaColumnSelect(documento, "description", "Nome prodotto")}
        ${renderSchemaColumnSelect(documento, "ean", "Codice EAN", { optional: ruoloFornitore })}
        ${ruoloFornitore ? renderSchemaColumnSelect(documento, "unit_price_net", "Prezzo netto") : renderSchemaColumnSelect(documento, "last_unit_price", "Ultimo prezzo")}
        ${ruoloFornitore ? `
          <div class="field">
            <label for="${escapeHtml(idCampoSchema(documento.profileId, "factorMode"))}">Pezzi per collo</label>
            <select id="${escapeHtml(idCampoSchema(documento.profileId, "factorMode"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="factorMode">
              <option value="column" ${fattoreFisso ? "" : "selected"}>Presi da una colonna</option>
              <option value="fixed" ${fattoreFisso ? "selected" : ""}>Valore uguale per tutte le righe</option>
            </select>
          </div>
          ${fattoreFisso
            ? `<div class="field"><label for="${escapeHtml(idCampoSchema(documento.profileId, "piecesPerCartonDefault"))}">Pezzi per collo</label><input id="${escapeHtml(idCampoSchema(documento.profileId, "piecesPerCartonDefault"))}" class="input" type="number" min="0.0001" step="any" value="${escapeHtml(valore.piecesPerCartonDefault || 1)}" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="piecesPerCartonDefault"></div>`
            : renderSchemaColumnSelect(documento, "pieces_per_carton", "Colonna pezzi per collo")}
          ${documento.format === "csv" ? "" : renderSchemaColumnSelect(documento, "", "Colonna in cui scrivere l’ordine", { order: true })}
        ` : renderSchemaColumnSelect(documento, "suggested_colli", "Colli suggeriti", { optional: true })}
      </div>
      <details class="schema-optional" ${apribile(`altre-colonne:${documento.profileId}`)}>
        <summary>Altre colonne</summary>
        <div class="schema-fields">
          ${ruoloFornitore ? renderSchemaColumnSelect(documento, "supplier_code", "Codice fornitore", { optional: true }) : ""}
          ${renderSchemaColumnSelect(documento, "unit", "Unità di misura", { optional: true })}
          ${renderSchemaColumnSelect(documento, "vat", "IVA", { optional: true })}
          ${ruoloFornitore ? renderDisponibilita(documento, valore) : ""}
          ${ruoloFornitore ? renderCondizioniCommerciali(documento, valore) : ""}
        </div>
      </details>
      ${renderSchemaValidation(documento)}
    </article>`;
}

// The "Availability" column alone says nothing: without a list of which
// values mean "available", picking the column has no effect and unavailable
// rows can still win the comparison. That list is written by the user, who
// has the price list in front of them — hardcoding it ("SI", "S", "X",
// "DISP"...) would wire one supplier's convention onto every other.
//
// The field only appears once a column is chosen: without a column there's
// nothing to declare.
function renderDisponibilita(documento, valore) {
  const colonna = renderSchemaColumnSelect(documento, "availability", "Disponibilità", { optional: true });
  if (!valore.columns?.availability) return colonna;
  const identificativo = escapeHtml(idCampoSchema(documento.profileId, "availableValues"));
  return `
    ${colonna}
    <div class="field">
      <label for="${identificativo}">Valori che significano disponibile</label>
      <input id="${identificativo}" class="input" type="text" value="${escapeHtml(valore.availableValues || "")}" placeholder="SI" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="availableValues" autocomplete="off">
      <p class="settings-note">Separati da una virgola, come li scrive lui: «SI, S». Tutto il resto conta come non disponibile.</p>
    </div>`;
}

// Commercial conditions are a column like any other — "where it writes its
// offers" — so they live under "Other columns" rather than a new panel: same
// gesture as the fields above it. This lets a new supplier be fully
// configured from this page, promotions included, instead of leaving them to
// be set by hand in the adapter registry.
//
// The extra two questions only appear when actually needed: the layout is
// asked once the column exists, and the two trailing columns only for the
// "block of rows" layout, the only one that requires them — the service
// rejects the declaration without them, and asking always would show four
// fields where one suffices.
const FORME_DELLE_OFFERTE = [
  ["", "Ogni riga porta la sua offerta"],
  ["blocchi", "Un blocco di righe per ogni offerta"],
];

function renderCondizioniCommerciali(documento, valore) {
  const colonna = renderSchemaColumnSelect(documento, "promotion_text", "Dove scrive le sue offerte", { optional: true });
  if (!valore.columns?.promotion_text) return colonna;
  const forma = String(valore.commercialLayout || "");
  return `
    ${colonna}
    <div class="field">
      <label for="${escapeHtml(idCampoSchema(documento.profileId, "commercialLayout"))}">Come sono scritte</label>
      <select id="${escapeHtml(idCampoSchema(documento.profileId, "commercialLayout"))}" class="select" data-schema-document="${escapeHtml(documento.profileId)}" data-schema-field="commercialLayout">
        ${FORME_DELLE_OFFERTE.map(([chiave, etichetta]) => `<option value="${escapeHtml(chiave)}" ${forma === chiave ? "selected" : ""}>${escapeHtml(etichetta)}</option>`).join("")}
      </select>
    </div>
    ${forma === "blocchi" ? `
      ${renderSchemaColumnSelect(documento, "reward_description", "Nome dell’articolo in omaggio", { optional: true })}
      ${renderSchemaColumnSelect(documento, "discount", "Codici delle righe", { optional: true })}
    ` : ""}`;
}

// The title says which of the two situations is happening, and both when
// both apply — a generic "unrecognized columns" count would be half wrong
// when some documents are new and others just changed.
function titoloDellaMappatura(nuovi, cambiati) {
  const nome = (elenco) => `«${String(elenco[0].fileName || "")}»`;
  if (!cambiati.length) {
    return nuovi.length === 1
      ? `Non riconosco le colonne di ${nome(nuovi)}`
      : `Non riconosco le colonne di ${formatInteger(nuovi.length)} documenti`;
  }
  if (!nuovi.length) {
    return cambiati.length === 1
      ? `Il listino ${nome(cambiati)} è cambiato: controlla le colonne`
      : `${formatInteger(cambiati.length)} listini che conosco sono cambiati: controlla le colonne`;
  }
  return `${contati(nuovi.length, "listino nuovo", "listini nuovi")} e ${contati(cambiati.length, "listino cambiato", "listini cambiati")}: dimmi dove sono le colonne`;
}

// The same column picker, opened from a document card instead of a stopped
// pipeline. Title and actions differ: nothing here is unrecognized, so it
// just saves, and the user reruns the comparison whenever they choose.
function renderColonneDocumento() {
  const mappatura = state.schemaMapping;
  const nome = String(mappatura.documento || "");
  const documenti = mappatura.data?.documents || [];
  const corpo = mappatura.loading
    ? '<div class="loading-card loading-card--small"><span class="spinner" aria-hidden="true"></span><strong>Preparo l’anteprima…</strong></div>'
    : mappatura.error
      ? renderAlert({ title: "Anteprima non disponibile", message: mappatura.error, severity: "error", blocking: true })
      : documenti.map(renderSchemaDocument).join("");
  const pronto = !mappatura.loading && !mappatura.error && documenti.length;
  return `
    ${pageHeading(
      "Colonne del listino",
      `Dove stanno i dati dentro «${nome}». Cambiarle cambia i prezzi del confronto, quindi il confronto va rifatto: qui si salva soltanto la scelta.`,
      '<button class="button button--ghost button--small" type="button" data-action="chiudi-colonne">Torna ai documenti</button>',
    )}
    ${mappatura.chiedendoUscita ? renderAlert({
      title: "Esci senza salvare le colonne?",
      message: "Quello che hai impostato qui — foglio, righe e colonne — non è ancora stato salvato e va perso. Le colonne di prima restano quelle che sono.",
      severity: "warning",
      blocking: false,
    }) + `
      <div class="schema-mapping__actions">
        <button class="button button--danger-soft" type="button" data-action="esci-dalle-colonne">Esci senza salvare</button>
        <button class="button button--ghost" type="button" data-action="resta-nelle-colonne">Resta qui</button>
      </div>` : ""}
    ${/* No heading here: the file name is already in the page title and at
         the top of the document card, and the card itself states its status. */ ""}
    <section class="schema-mapping" aria-label="Colonne del documento">
      ${corpo}
      ${pronto ? `
        <div class="schema-mapping__actions">
          <button class="button button--secondary" type="button" data-action="prova-colonne" ${mappatura.validating || mappatura.confirming ? "disabled" : ""}>${mappatura.validating ? "Controllo…" : "Prova le colonne"}</button>
          <button class="button button--primary" type="button" data-action="salva-colonne" ${mappatura.validating || mappatura.confirming ? "disabled" : ""}>${mappatura.confirming ? "Salvataggio…" : "Salva le colonne"}</button>
        </div>
        <p class="schema-mapping__nota">Le colonne nuove le usa il prossimo confronto: quello che vedi nelle pagine 2 e 3 è stato letto con quelle di prima.</p>
      ` : ""}
    </section>`;
}

function renderSchemaMappingWizard() {
  const mappatura = state.schemaMapping;
  if (mappatura.loading) return '<div class="loading-card loading-card--small"><span class="spinner" aria-hidden="true"></span><strong>Preparo l’anteprima dei file…</strong></div>';
  // A failed "Prova le colonne" keeps the wizard on screen, error and retry
  // button next to the fields, instead of forcing a reload to start over.
  const documenti = mappatura.data?.documents || [];
  if (!documenti.length) {
    return renderAlert({
      title: "Non riesco a mostrarti le colonne",
      message: mappatura.error || "Ricarica la pagina e riprova.",
      severity: "error",
      blocking: true,
    });
  }
  const tuttiControllati = documenti.every((documento) => state.schemaMapping.results[String(documento.profileId || "")]?.ok);
  const cambiati = documenti.filter((documento) => String(documento.reason?.state || "") === "VARIATO");
  const nuovi = documenti.filter((documento) => String(documento.reason?.state || "") !== "VARIATO");
  const titolo = titoloDellaMappatura(nuovi, cambiati);
  return `
    <section class="schema-mapping" aria-labelledby="schema-mapping-title">
      <div class="schema-mapping__intro">
        <div><h3 id="schema-mapping-title">${escapeHtml(titolo)}</h3><p>Dimmi dove sono nome, prezzo e pezzi per collo. Controllo i dati prima di usarli e poi il confronto riparte da solo.</p></div>
        ${renderBadge(contati(documenti.length, "documento", "documenti"), "warning")}
      </div>
      ${documenti.map(renderSchemaDocument).join("")}
      ${mappatura.error ? renderAlert({
        title: "Le colonne non vanno ancora bene",
        message: mappatura.error,
        severity: "error",
        blocking: false,
      }) : ""}
      <div class="schema-mapping__actions">
        <button class="button button--secondary" type="button" data-action="validate-schemas" ${mappatura.validating || mappatura.confirming ? "disabled" : ""}>${mappatura.validating ? "Controllo…" : "Prova le colonne"}</button>
        <button class="button button--primary" type="button" data-action="confirm-schemas" ${mappatura.validating || mappatura.confirming ? "disabled" : ""}>${mappatura.confirming ? "Avvio…" : "Conferma e riparti col confronto"}</button>
      </div>
      <p class="schema-mapping__nota">${tuttiControllati
        ? "Colonne provate: quello che vedi qui sopra è quello che entrerà nel confronto."
        : "«Prova le colonne» è facoltativo: mostra quante righe verrebbero lette prima di spendere i due minuti del confronto. Gli stessi controlli vengono comunque rifatti quando confermi."}</p>
    </section>`;
}

// "Running for N minutes", rounded to the minute: whoever watches a stalled
// bar wants to know if the wait is normal, not to time it precisely.
function daQuantoGira(iniziatoIl) {
  const inizio = Date.parse(String(iniziatoIl || ""));
  if (!Number.isFinite(inizio)) return "";
  const minuti = Math.floor((Date.now() - inizio) / 60000);
  // No message under a minute — the bar still moves on its own by then.
  // No upper cap either: "running for 47 minutes" is exactly what's needed
  // when something is actually stuck.
  if (minuti < 1) return "";
  return `In corso da ${contati(minuti, "minuto", "minuti")}.`;
}

function renderAvanzamentoPipeline() {
  const stato = state.pipeline.stato;
  if (!stato || !state.pipeline.chiesto) return "";
  // The progress bar is for a recompute that exists. `IN_ATTESA` has none —
  // neither when nothing has run yet, nor when documents changed since the
  // last comparison — and a bar at zero with every phase "pending" would be
  // noise. `renderCambiamentoDocumenti()` covers that case instead.
  if (String(stato.stato || "") === "IN_ATTESA") return "";
  const fasi = Array.isArray(stato.fasi) ? stato.fasi : [];
  const percento = Number(stato.avanzamento?.percento || 0);
  const fermata = stato.fermata;
  const richiedeMappatura = schemaMappingRequired(stato);
  const numeri = renderNumeriPipeline(stato.numeri || {});
  // The "comparison updated" message duplicates the numbers already shown in
  // the row below it, so it's suppressed once the pipeline finished
  // successfully with numbers to show. It stays where it's useful: while
  // running, when stopped, and when the numbers are missing — there it's the
  // only thing that says how it went.
  const messaggio = String(stato.messaggio || "");
  const diciIlMessaggio = Boolean(messaggio) && !richiedeMappatura
    && !(String(stato.stato || "") === "COMPLETATO" && numeri && !fermata);
  // Pipeline warnings also surface inside the comparison, and the panel at
  // the top of the page already shows them — printing them again here would
  // duplicate the same warning. Only warnings that panel is actually
  // rendering are deduplicated: while the mapping wizard is open,
  // `renderSourceWarnings` only shows blocking ones, so filtering out the
  // rest here too would make them vanish from both places at once.
  const gia = new Set(avvisiVisibiliInCima().map((avviso) => avviso.code).filter(Boolean));
  const avvisi = asArray(stato.avvisi)
    .map((avviso) => normalizeIssue(avviso, `pipeline-${avviso?.code || ""}`))
    .filter((avviso) => !avviso.code || !gia.has(avviso.code));
  // Once done, a full bar just draws "it worked" instead of stating it, so
  // it's hidden. It's needed while the pipeline runs, and when it has
  // stopped — there it says how far it got.
  const barra = String(stato.stato || "") === "COMPLETATO" && !fermata
    ? ""
    : `<div class="pipeline-progress__bar"><div class="pipeline-progress__fill" style="width: ${percento}%"></div></div>`;
  // Elapsed running time. One phase is by far the longest in the pipeline and
  // holds the bar at a fixed percentage for most of that time; a stalled bar
  // with no message reads as "it's stuck", inviting the user to close the app
  // mid-run. The elapsed time is computed from the service's own timestamp
  // (`iniziatoIl`), not a `Date.now()` fixed on page load — otherwise a
  // browser reload mid-run would reset the counter to zero.
  const daQuanto = String(stato.stato || "") === "IN_CORSO" ? daQuantoGira(stato.iniziatoIl) : "";
  return `
    <div class="pipeline-progress">
      ${barra}
      ${daQuanto ? `<p class="pipeline-progress__tempo">${escapeHtml(daQuanto)}</p>` : ""}
      ${state.pipeline.contattoPerso ? renderAlert({
        title: "Non so più a che punto è",
        message: state.pipeline.contattoPerso,
        severity: "warning",
        blocking: false,
      }) : ""}
      ${diciIlMessaggio ? `<p class="operation-message">${escapeHtml(messaggio)}</p>` : ""}
      ${numeri}
      <details class="pipeline-details" ${apribile("ricalcolo")}>
        <summary>Dettagli del confronto</summary>
        <ol class="pipeline-phases">
          ${fasi.map((fase) => `
            <li class="pipeline-phase pipeline-phase--${escapeHtml(String(fase.stato || "IN_ATTESA").toLowerCase())}">
              <span>${escapeHtml(String(fase.titolo || fase.nome || ""))}</span>
              ${renderBadge(
                fase.stato === "COMPLETATO" ? "Fatta" : fase.stato === "IN_CORSO" ? "In corso" : fase.stato === "ERRORE" ? "Ferma" : "In attesa",
                TONO_FASE[fase.stato] || "neutral",
              )}
              ${fase.dettaglio ? `<small>${escapeHtml(String(fase.dettaglio))}</small>` : ""}
            </li>`).join("")}
        </ol>
      </details>
      ${/* No separate alert here: it would duplicate the wizard's own title
           just below, which already states which of the two cases applies. */ ""}
      ${!richiedeMappatura && fermata ? renderAlert({
        title: "Il confronto si è fermato",
        // "The previous comparison is still there" is said here, at the
        // moment the user wonders if the run's work is lost, rather than
        // permanently under the button.
        message: `${String(fermata.message || "")}${confrontoDisponibile() ? " Il confronto di prima è ancora al suo posto." : ""}`,
        severity: "error",
        blocking: true,
      }) : ""}
      ${/* `renderAvvisi` groups warnings that share a title into one panel,
           the same grouping used everywhere else in the app — printing them
           one by one here would turn ten identical warnings into ten boxes. */ ""}
      ${renderAvvisi(avvisi, "catena")}
      ${richiedeMappatura ? renderSchemaMappingWizard() : ""}
    </div>`;
}

function renderPipelinePanel() {
  const situazione = statoPaginaImporta();
  const inCorso = situazione === "RICALCOLO_IN_CORSO";
  const richiedeMappatura = schemaMappingRequired();
  const comando = comandoConfronto(situazione);
  // The panel title states what the panel is about; the button states what
  // it does — using the same wording for both read as two separate commands.
  //
  // The explanation is only shown before a first comparison exists: what
  // pressing the button does and how long it takes. Once a comparison
  // exists, the same sentence would just be a caption on a button pressed
  // weekly, and the "originals are untouched" promise is already stated
  // under the page title.
  const spiegazione = confrontoDisponibile()
    ? ""
    : "Legge i documenti caricati, li confronta e prepara le offerte: ci vogliono alcuni minuti. Nessun ordine viene inviato.";
  // The action button sits in the panel header, next to what it acts on.
  return `
    <section class="panel import-panel pipeline-panel">
      <div class="panel__header">
        <div>
          <h3>Confronto dei listini</h3>
          ${spiegazione ? `<p>${escapeHtml(spiegazione)}</p>` : ""}
        </div>
        <div class="button-row">
          ${inCorso ? renderBadge("in corso", "info") : ""}
          ${richiedeMappatura ? "" : `<button class="button button--${escapeHtml(comando.tono)}" type="button" data-action="avvia-pipeline" ${comando.disabilitato ? "disabled" : ""}>
            ${escapeHtml(comando.etichetta)}
          </button>`}
        </div>
      </div>
      ${richiedeMappatura || !comando.nota ? "" : `<p class="button-hint">${escapeHtml(comando.nota)}</p>`}
      ${state.pipeline.errore ? renderAlert({ title: "Confronto non avviato", message: state.pipeline.errore, severity: "error", blocking: false }) : ""}
      ${renderAvanzamentoPipeline()}
    </section>`;
}

// "Start a new comparison": clears the management file and every price list
// in one action, instead of the operator removing each of them by hand before
// uploading the new ones. Skipping that reset isn't obvious either — two
// price lists from the same supplier only let one into the comparison,
// picked by file modification date, so a leftover old file can silently win.
//
// Placed near "Settings", not among the upload commands: it isn't a way to
// upload a document, it's the gesture that starts a new week's run.
function renderComandoNuovaComparazione(bloccato) {
  // Nothing to clear: no documents and no comparison. A command that would
  // do nothing is disabled rather than reporting "done" on an empty state.
  const c_e_qualcosa = state.review.files.length > 0 || state.review.products.length > 0;
  const spento = bloccato || !c_e_qualcosa || state.nuovaComparazione.inCorso || state.nuovaComparazione.chiedendo;
  return `<button class="button button--ghost button--small" type="button" data-action="nuova-comparazione" ${spento ? "disabled" : ""}>${
    state.nuovaComparazione.inCorso ? "Attendere…" : "Inizia nuova comparazione"
  }</button>`;
}

// The confirmation is one line, not a modal: uploaded documents are never
// modified in place, so what disappears is only the app's own working copies,
// and the gesture doesn't need a full-screen dialog. Both parts of the line
// matter though: the count, because the command does delete something real,
// and the reassurance, because the real worry isn't what gets removed but
// what the user doesn't know is safe. Confirmations and pending orders
// survive, and that has to be said here.
function renderConfermaNuovaComparazione() {
  if (!state.nuovaComparazione.chiedendo) return "";
  // Not filtered by `role`: `normalizeReview` doesn't copy it — the page
  // recognizes the management file by `kind`/`supplier` (`isManagementFile`),
  // not by a role field. `files` is already exactly the list of cards shown
  // above, so this count matches what the user can verify by eye.
  const documenti = state.review.files.length;
  const quanti = documenti
    ? `e tolgo ${escapeHtml(contati(documenti, "documento caricato", "documenti caricati"))}`
    : "e riparto dai documenti";
  return `
    <div class="conferma-riga" role="alert">
      <span><strong>Chiudo la comparazione di adesso ${quanti}.</strong> I file sul tuo computer non si toccano; le conferme che hai dato e gli ordini di cui aspetti la merce restano.</span>
      <span class="conferma-riga__comandi">
        <button type="button" class="button button--danger-soft" data-action="conferma-nuova-comparazione" ${state.nuovaComparazione.inCorso ? "disabled" : ""}>${state.nuovaComparazione.inCorso ? "Attendere…" : "Sì, comincia"}</button>
        <button type="button" class="button button--ghost" data-action="annulla-nuova-comparazione" ${state.nuovaComparazione.inCorso ? "disabled" : ""}>Annulla</button>
      </span>
    </div>`;
}

function renderUploadStep() {
  // Reviewing a price list's columns is its own task: it takes over the
  // page, and exits back where it was entered from, instead of mixing a
  // preview table into the document list.
  if (colonneAperteAMano()) return renderColonneDocumento();
  const blockingFiles = state.review.files.filter((file) => fileStatus(file).tone === "danger").length;
  const managementFiles = state.review.files.filter(isManagementFile);
  const supplierFiles = state.review.files.filter((file) => !isManagementFile(file));
  const situazione = statoPaginaImporta();
  const continua = comandoContinua(situazione);
  // Upload commands are locked during a running recompute: changing the
  // documents underneath it would invalidate the comparison in progress.
  const bloccato = situazione === "RICALCOLO_IN_CORSO";
  return `
    ${pageHeading(
      "1. Importa i dati",
      "Carica prima l’elenco dei prodotti da ordinare, poi i listini dei fornitori. I documenti originali non vengono modificati.",
      `${renderComandoNuovaComparazione(bloccato)}<button class="button button--ghost button--small" type="button" data-action="apri-impostazioni">Impostazioni</button>`,
    )}
    ${renderConfermaNuovaComparazione()}
    ${/* The pending-orders question also appears here, not only on page 2:
         "did it arrive?" is exactly the question that comes up when starting
         a new comparison. Shown only when there's no comparison to look at
         yet — right after the reset, or on first launch — since once a
         comparison exists, page 1 is about documents and the question stays
         where it was. */ ""}
    ${state.review.products.length ? "" : renderPendingOrdersPanel()}
    ${renderCambiamentoDocumenti()}
    ${state.uploadMessage ? renderAlert({ title: "Documenti caricati", message: state.uploadMessage, severity: "info", blocking: false }) : ""}
    ${renderSourceWarnings(1)}
    <div class="import-sections" style="margin-top: ${state.uploadMessage ? "1rem" : "0"}">
      <section class="panel import-panel">
        <div class="panel__header">
          <div><h3>Prodotti da ordinare</h3><p>Elenco esportato dal gestionale</p></div>
          ${managementFiles.length ? renderBadge("Elenco presente", "success") : renderBadge("Da caricare", "warning")}
        </div>
        ${managementFiles.length ? `<div class="file-grid">${managementFiles.map(renderFileCard).join("")}</div>` : '<div class="empty-state empty-state--small"><div><strong>Nessun elenco caricato</strong><p>Seleziona il documento che contiene i prodotti da valutare.</p></div></div>'}
        <div class="dropzone dropzone--compact${managementFiles.length ? " dropzone--riga" : ""}${bloccato ? " is-locked" : ""}" data-dropzone data-upload-role="management">
          <div>
            <strong>Carica o sostituisci l’elenco</strong>
            <p>${bloccato ? "Aspetta che il confronto abbia finito: un elenco nuovo adesso cambierebbe il risultato." : "Trascina qui il documento oppure sceglilo dal computer."}</p>
            <label class="button button--secondary${bloccato ? " is-disabled" : ""}" for="file-picker-management">Scegli l’elenco dei prodotti</label>
            <input id="file-picker-management" type="file" accept=".xlsx,.xls,.csv" data-picker-role="management" ${bloccato ? "disabled" : ""}>
          </div>
        </div>
        ${renderPendingFiles("management")}
      </section>

      <section class="panel import-panel">
        <div class="panel__header">
          <div><h3>Listini dei fornitori</h3><p>Un listino per ciascun fornitore</p></div>
          ${blockingFiles ? renderBadge(`${formatInteger(blockingFiles)} da correggere`, "danger") : renderBadge(contati(supplierFiles.length, "presente", "presenti"), supplierFiles.length ? "success" : "warning")}
        </div>
        ${supplierFiles.length ? `<div class="file-grid">${supplierFiles.map(renderFileCard).join("")}</div>` : '<div class="empty-state empty-state--small"><div><strong>Nessun listino caricato</strong><p>Puoi aggiungere più fornitori insieme.</p></div></div>'}

        <div class="dropzone dropzone--compact${supplierFiles.length ? " dropzone--riga" : ""}${bloccato ? " is-locked" : ""}" data-dropzone data-upload-role="suppliers">
          <div>
            <strong>Carica o sostituisci altri listini</strong>
            <p>${bloccato ? "Aspetta che il confronto abbia finito: un listino nuovo adesso cambierebbe il risultato." : "Trascina qui uno o più listini."}</p>
            <label class="button button--secondary${bloccato ? " is-disabled" : ""}" for="file-picker-suppliers">Scegli i listini</label>
            <input id="file-picker-suppliers" type="file" accept=".xlsx,.xls,.csv" multiple data-picker-role="suppliers" ${bloccato ? "disabled" : ""}>
          </div>
        </div>
        ${renderPendingFiles("suppliers")}
      </section>
    </div>
    ${renderDiscardedRowsPanel()}
    ${renderPipelinePanel()}
    <div class="page-actions">
      <button class="button button--${escapeHtml(continua.tono)}" type="button" data-action="next" ${continua.disabilitato ? "disabled" : ""}>${escapeHtml(continua.etichetta)}</button>
    </div>
    ${continua.nota ? `<p class="button-hint button-hint--end">${escapeHtml(continua.nota)}</p>` : ""}`;
}

function productTypeLabel(product) {
  if (product.itemType === "display") return "Espositore";
  if (product.itemType === "kit") return "Kit";
  return "Prodotto";
}

function productTypeTone(product) {
  return product.itemType === "display" || product.itemType === "kit" ? "info" : "neutral";
}

function orderUnitSingular(label) {
  const normalized = String(label || "unità").trim().toLocaleLowerCase("it");
  const known = {
    colli: "collo",
    cartoni: "cartone",
    espositori: "espositore",
    kit: "kit",
    pezzi: "pezzo",
    unità: "unità",
  };
  return known[normalized] || String(label || "unità");
}

// Italian alphabetical order, with digit runs compared numerically: "NEVAL
// 10ML" before "NEVAL 100ML". `sensitivity: "base"` treats accented and
// uppercase letters like their plain lowercase form instead of sorting them
// after everything else.
const CONFRONTO_ALFABETICO = { sensitivity: "base", numeric: true };

function sortedProducts(products) {
  const verso = state.filters.sort === "nome-desc" ? -1 : 1;
  if (state.filters.sort !== "nome" && state.filters.sort !== "nome-desc") return products;
  // Copy first: `sort` mutates in place, and this array is the same one held in `state.review`.
  return products.slice().sort((a, b) => {
    const nome = String(a.name || "").localeCompare(String(b.name || ""), "it", CONFRONTO_ALFABETICO);
    // Barcode breaks ties on name: two rows with the same name are two
    // different items, and the order must stay stable across re-renders.
    if (nome !== 0) return nome * verso;
    return String(a.ean || "").localeCompare(String(b.ean || ""), "it", CONFRONTO_ALFABETICO) * verso;
  });
}

// The "Show" filter entries, each with the question it answers. One place
// only: the same predicate both counts rows in the label and filters the
// list, so the two never drift apart.
//
// Kept to the four questions actually asked in front of five hundred rows:
// what to order, what needs confirming, what nobody has, what was set aside.
const FILTRI_PRODOTTO = {
  all: { etichetta: "Tutti i prodotti", tieni: () => true, sempre: true },
  ordered: { etichetta: "Da ordinare", tieni: (product) => orderQuantity(product) > 0, sempre: true },
  "to-confirm": { etichetta: "Da confermare", tieni: (product) => orderQuantity(product) > 0 && daConfermare(product) },
  // "Nobody has it" would be wrong for part of its own products.
  // `nessunaOffertaUtilizzabile` answers the real question — "can this be
  // ordered?" — where rejected offers count too: saying no to every supplier
  // leaves nothing to choose from either. But the products' price-list rows
  // did exist; the user removed them. These stay as two separate filters,
  // because the fix differs: one is sourced elsewhere, the other just needs
  // an answer revisited.
  missing: {
    etichetta: "Nessuno ce l’ha",
    tieni: (product) => orderQuantity(product) > 0
      && nessunaOffertaUtilizzabile(product) && rifiutatiDaTe(product) === 0,
  },
  rejected: {
    etichetta: "Hai risposto no",
    tieni: (product) => orderQuantity(product) > 0
      && nessunaOffertaUtilizzabile(product) && rifiutatiDaTe(product) > 0,
  },
  excluded: { etichetta: "Esclusi", tieni: isExcluded },
  pending: { etichetta: "Già ordinati", tieni: (product) => pendingOrdersFor(product).length > 0 },
  // Displays have no entry here: they're already covered by "Product type",
  // and two paths to the same list is the overlap this filter set was
  // trimmed to avoid. Manually added items had no way to find them, so they
  // get one.
  manual: { etichetta: "Aggiunti a mano", tieni: (product) => Boolean(product.addedManually) },
};

// Excluded products only show up under their own filter: they're products
// the user chose not to order, and mixing them back into the others would
// put them back among the decisions to make.
function passaIlFiltro(product, stato) {
  const voce = FILTRI_PRODOTTO[stato] || FILTRI_PRODOTTO.all;
  if (stato !== "excluded" && isExcluded(product)) return false;
  return voce.tieni(product);
}

// How many products each filter entry would show. Counted over the full
// list, not the one already narrowed by search and type: a count that shifts
// while typing in the search box would stop telling how much work is left.
function conteggiDelFiltro() {
  const conteggi = {};
  for (const stato of Object.keys(FILTRI_PRODOTTO)) conteggi[stato] = 0;
  for (const product of state.review?.products || []) {
    for (const stato of Object.keys(FILTRI_PRODOTTO)) {
      if (passaIlFiltro(product, stato)) conteggi[stato] += 1;
    }
  }
  return conteggi;
}

function filteredProducts() {
  const search = state.filters.search.trim().toLocaleLowerCase("it");
  const activePromotion = state.filters.promotionId ? findPromotion(state.filters.promotionId) : null;
  const promotionProductIds = activePromotion ? new Set(promotionMatchedProductIds(activePromotion)) : null;
  return sortedProducts(state.review.products.filter((product) => {
    if (promotionProductIds && !promotionProductIds.has(String(product.id))) return false;
    if (search) {
      const haystack = `${product.name} ${product.ean} ${product.components.map((component) => component.name).join(" ")}`.toLocaleLowerCase("it");
      if (!haystack.includes(search)) return false;
    }
    if (state.filters.type !== "all" && product.itemType !== state.filters.type) return false;
    return passaIlFiltro(product, state.filters.status);
  }));
}

const NOMI_DEI_TIPI = { product: "Prodotti singoli", display: "Espositori", kit: "Kit" };

// The product-type field only appears when there's more than one type to
// choose from; with a single type, it's a control with nothing to decide.
function renderTypeFilter() {
  const presenti = [...new Set((state.review?.products || []).map((product) => String(product.itemType || "product")))];
  if (presenti.length < 2 && state.filters.type === "all") return "";
  const voci = presenti.filter((tipo) => NOMI_DEI_TIPI[tipo]);
  if (state.filters.type !== "all" && !voci.includes(state.filters.type)) voci.push(state.filters.type);
  return `
      <div class="field">
        <label for="type-filter">Tipo di prodotto</label>
        <select id="type-filter" class="select" data-filter="type">
          <option value="all" ${state.filters.type === "all" ? "selected" : ""}>Tutti</option>
          ${voci.map((tipo) => `<option value="${escapeHtml(tipo)}" ${state.filters.type === tipo ? "selected" : ""}>${escapeHtml(NOMI_DEI_TIPI[tipo] || tipo)}</option>`).join("")}
        </select>
      </div>`;
}

// An entry that would show nothing isn't drawn: "Excluded" only makes sense
// once something is excluded, "Already ordered" once an order is pending.
// "All products" and "To order" always stay — they're the two ways of
// viewing the whole list — and the currently selected entry always stays too,
// even at zero: a menu that drops the selected filter would misstate what's
// being shown.
function renderStatusFilter() {
  const conteggi = conteggiDelFiltro();
  const voci = Object.entries(FILTRI_PRODOTTO)
    .filter(([stato, voce]) => voce.sempre || conteggi[stato] > 0 || state.filters.status === stato);
  return `
      <div class="field">
        <label for="status-filter">Mostra</label>
        <select id="status-filter" class="select" data-filter="status">
          ${voci.map(([stato, voce]) => `<option value="${escapeHtml(stato)}" ${state.filters.status === stato ? "selected" : ""}>${escapeHtml(voce.etichetta)} (${formatInteger(conteggi[stato])})</option>`).join("")}
        </select>
      </div>`;
}

function renderToolbar() {
  const hasManagementQuantities = state.review.products.some((product) => product.quantitySource === "gestionale" && orderQuantity(product) > 0);
  return `
    <div class="toolbar">
      <div class="field">
        <label for="product-search">Cerca</label>
        <input id="product-search" class="input" type="search" value="${escapeHtml(state.filters.search)}" placeholder="Nome, EAN o componente…" data-filter="search" data-focus-key="product-search">
      </div>
      ${renderTypeFilter()}
      ${renderStatusFilter()}
      <div class="field">
        <label for="sort-filter">Ordina per</label>
        <select id="sort-filter" class="select" data-filter="sort">
          <option value="gestionale" ${state.filters.sort === "gestionale" ? "selected" : ""}>Ordine del gestionale</option>
          <option value="nome" ${state.filters.sort === "nome" ? "selected" : ""}>Nome (dalla A alla Z)</option>
          <option value="nome-desc" ${state.filters.sort === "nome-desc" ? "selected" : ""}>Nome (dalla Z alla A)</option>
        </select>
      </div>
      ${hasManagementQuantities ? `
      <div class="toolbar__reset">
        ${/* This only clears products with `quantitySource === "gestionale"`
             — quantities never touched by hand, since editing one marks it
             "user" — but the button alone doesn't say that, so the label
             below spells out what stays untouched. */ ""}
        <button type="button" class="button button--ghost" data-action="reset-suggested-quantities">Azzera le quantità proposte dal gestionale</button>
        <span class="toolbar__reset-nota">Le quantità che hai scritto tu restano.</span>
      </div>` : ""}
    </div>`;
}

function renderComponents(product) {
  if (!product.components.length) return "";
  const total = product.components.reduce((sum, component) => sum + component.quantity, 0);
  return `
    <details class="nested-details" ${apribile(`composizione:${product.id}`)}>
      <summary>Composizione espositore · ${formatInteger(total)} pezzi</summary>
      <table class="component-table">
        <thead><tr><th>Componente</th><th>EAN</th><th>Pezzi</th></tr></thead>
        <tbody>
          ${product.components.map((component) => `
            <tr>
              <td>${escapeHtml(component.name)}</td>
              <td>${component.ean ? escapeHtml(component.ean) : "—"}</td>
              <td>${formatInteger(component.quantity)}</td>
            </tr>`).join("")}
        </tbody>
      </table>
    </details>`;
}

// What's missing from a proposed row before it can be accepted. Matches the
// two conditions the local service checks for `available` (in
// `build_review_data.offer_from_match`): a price above zero and a
// pieces-per-carton figure. Naming which one is missing matters — they're two
// different defects in the price list and are fixed in two different ways.
function percheNonSiPuoAccettare(candidate, issue) {
  const fornitore = candidate.supplierName || supplierName(issue.supplierId) || "il fornitore";
  const manca = [
    candidate.orderUnitPriceNet > 0 || candidate.unitPriceNet > 0 ? "" : "il prezzo",
    candidate.quantityFactor > 0 ? "" : "quanti pezzi ci sono in un collo",
  ].filter(Boolean);
  if (!manca.length) return `Questa riga di ${fornitore} non è ordinabile così com'è.`;
  return `Non si può rispondere «sì»: la riga di ${fornitore} non dice ${manca.join(" né ")}, e senza l'ordine non si può calcolare.`;
}

function renderProductIssues(product) {
  // The confirmation question already has its own control right under the
  // offers; repeating it as a red error above adds text without adding an action.
  const issues = issuesForProduct(product).filter((issue) => issue.id !== `${product.id}-confirmation`);
  if (!issues.length) return "";
  return `<div class="product-issues">${issues.map((issue) => {
    const candidate = issue.candidate;
    if (issue.code !== "RIFIUTO_CON_CANDIDATO_FORTE" || !candidate || !issue.candidateKey) {
      return renderAlert(issue);
    }
    const answerKey = `${product.id}:${issue.supplierId}:${issue.candidateKey}`;
    const busy = Boolean(state.matches.answering);
    const answering = state.matches.answering === answerKey;
    const meta = [
      candidate.ean ? `EAN ${candidate.ean}` : "",
      candidate.supplierCode ? `codice ${candidate.supplierCode}` : "",
      candidate.quantityFactor > 0 ? contati(candidate.quantityFactor, "pezzo per collo", "pezzi per collo") : "",
    ].filter(Boolean).join(" · ");
    // "Yes" only renders when the row is actually acceptable: the local
    // service rejects one without a usable price and packaging, so a button
    // that errors on every press isn't a real question. When it's missing,
    // the page instead says what's missing, and "No" — always answerable —
    // stays available.
    return `
      <section class="candidate-check" role="status">
        <div class="candidate-check__body">
          <span class="candidate-check__supplier">${escapeHtml(candidate.supplierName || supplierName(issue.supplierId))}</span>
          <strong>${escapeHtml(candidate.description || "Prodotto proposto")}</strong>
          ${meta ? `<span>${escapeHtml(meta)}</span>` : ""}
          ${candidate.orderUnitPriceNet > 0 ? `<span>${formatEuro(candidate.orderUnitPriceNet)} per collo · ${formatEuro(candidate.unitPriceNet)} per pezzo</span>` : ""}
          ${candidate.rationale ? `<span class="candidate-check__nota">L’analisi automatica l’ha scartata: ${escapeHtml(candidate.rationale)}</span>` : ""}
          ${candidate.available ? "" : `<span class="candidate-check__nota">${escapeHtml(percheNonSiPuoAccettare(candidate, issue))}</span>`}
        </div>
        <div class="candidate-check__actions">
          <span>È lo stesso articolo?</span>
          ${candidate.available ? `<button class="button button--success" type="button" data-action="answer-candidate" data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(issue.supplierId)}" data-candidate-key="${escapeHtml(issue.candidateKey)}" data-accepted="true" ${busy ? "disabled" : ""}>${answering ? "Attendere…" : "Sì, è lo stesso"}</button>` : ""}
          <button class="button button--secondary" type="button" data-action="answer-candidate" data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(issue.supplierId)}" data-candidate-key="${escapeHtml(issue.candidateKey)}" data-accepted="false" ${busy ? "disabled" : ""}>No, non è lo stesso</button>
        </div>
        ${issue.technicalMessage ? `<details class="candidate-check__details" ${apribile(`perche:${product.id}:${issue.supplierId}`)}><summary>Perché compare?</summary><p>${escapeHtml(issue.technicalMessage)}</p></details>` : ""}
      </section>`;
  }).join("")}</div>`;
}

// A "No" answer to a candidate match must stay reversible: the decision
// (`offer.candidateDecision`, kept by the local service alongside the
// proposed row) needs to be both visible and changeable for the rest of this
// comparison, or a mistaken click has no way back.
function renderCandidateDecisions(product) {
  const decise = product.offers.filter((offer) => {
    const decisione = String(offer.candidateDecision || "");
    return (decisione === "accepted" || decisione === "rejected") && offer.rejectedCandidate?.candidateKey;
  });
  if (!decise.length) return "";
  const busy = Boolean(state.matches.answering);
  return `
    <div class="candidate-decisions">
      ${decise.map((offer) => {
        const candidate = offer.rejectedCandidate;
        const accettata = String(offer.candidateDecision) === "accepted";
        const answerKey = `${product.id}:${offer.supplierId}:${candidate.candidateKey}`;
        const answering = state.matches.answering === answerKey;
        const nome = candidate.description || offer.description || "la riga proposta";
        const frase = accettata
          ? `Hai detto che «${nome}» di ${offer.supplierName} è lo stesso articolo: l’offerta è entrata nel confronto.`
          : `Hai detto che «${nome}» di ${offer.supplierName} NON è lo stesso articolo: quel fornitore resta fuori da questo prodotto.`;
        // Reverting to "Yes" only makes sense if the proposed row still has a
        // usable price and packaging: otherwise the local service would
        // reject the answer and the button would be an empty promise.
        const puoAccettare = accettata || candidate.available;
        return `
          <div class="candidate-decision${accettata ? " is-accepted" : " is-rejected"}">
            <span class="candidate-decision__text">${escapeHtml(frase)}</span>
            ${puoAccettare ? `
              <button class="button button--ghost" type="button" data-action="answer-candidate"
                data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(offer.supplierId)}"
                data-candidate-key="${escapeHtml(candidate.candidateKey)}" data-accepted="${accettata ? "false" : "true"}"
                ${busy ? "disabled" : ""}>${answering ? "Attendere…" : accettata ? "Cambio idea: non è lo stesso" : "Cambio idea: è lo stesso"}</button>
            ` : '<span class="candidate-decision__note">Non si può più tornare al «sì»: quella riga non ha prezzo e confezione utilizzabili.</span>'}
          </div>`;
      }).join("")}
    </div>`;
}

function quantityTitle(product) {
  if (product.itemType === "display") return "Numero di espositori";
  return "Colli da ordinare";
}

function renderQuantityControl(product) {
  const quantity = orderQuantity(product);
  const fromManagement = product.quantitySource === "gestionale";
  return `
    <div class="quantity-block">
      <div class="quantity-block__label-row">
        <span class="quantity-block__label">${escapeHtml(quantityTitle(product))}</span>
        ${fromManagement ? '<span class="source-tag" title="Valore proposto dalla colonna Colli del gestionale">valore dal gestionale</span>' : ""}
      </div>
      <div class="quantity-stepper" role="group" aria-label="${escapeHtml(quantityTitle(product))} per ${escapeHtml(product.name)}">
        <button type="button" class="quantity-stepper__button" data-action="change-quantity" data-product-id="${escapeHtml(product.id)}" data-delta="-1" data-focus-key="qty-meno-${escapeHtml(product.id)}" aria-label="Diminuisci la quantità di ${escapeHtml(product.name)}">−</button>
        <input class="quantity-input" type="number" min="0" max="100000" step="1" inputmode="numeric" value="${quantity}" data-product-quantity="${escapeHtml(product.id)}" data-focus-key="qty-${escapeHtml(product.id)}" aria-label="${escapeHtml(quantityTitle(product))} per ${escapeHtml(product.name)}">
        <button type="button" class="quantity-stepper__button" data-action="change-quantity" data-product-id="${escapeHtml(product.id)}" data-delta="1" data-focus-key="qty-piu-${escapeHtml(product.id)}" aria-label="Aumenta la quantità di ${escapeHtml(product.name)}">+</button>
      </div>
    </div>`;
}

// --- Orders not yet received, at the top of page 2 ------------------------
// An explicit question with two large buttons, not a `window.confirm`. Draws
// nothing, not even an empty panel, when nothing is pending.

// "Not received yet" doesn't dismiss the question, it defers it. When it
// comes back is decided by the local service (`askAgainAt`, a week after the
// answer), since it owns the order history; this only checks the clock. An
// order with no such date has never been answered and is due now.
function domandaRimandata(entry) {
  if (!entry.askAgainAt) return false;
  const ritorno = new Date(entry.askAgainAt);
  if (Number.isNaN(ritorno.getTime())) return false;
  return ritorno.getTime() > Date.now();
}

function visiblePendingOrders() {
  return asArray(state.history.pending)
    .filter((entry) => entry.orderId && !state.history.answered.has(entry.orderId) && !domandaRimandata(entry));
}

function pendingOrderSupplierLabel(entry) {
  return String(entry.supplierName || entry.supplier || "questo fornitore").toLocaleUpperCase("it");
}

function pendingOrderQuestion(entry) {
  const day = formatDayMonth(entry.createdAt);
  const supplier = pendingOrderSupplierLabel(entry);
  return day
    ? `Hai ricevuto la merce ordinata da ${supplier} il ${day}?`
    : `Hai ricevuto la merce ordinata da ${supplier}?`;
}

function pendingOrderMeta(entry) {
  const lines = `${formatInteger(entry.lineCount)} ${entry.lineCount === 1 ? "riga" : "righe"}`;
  return entry.totalNet > 0 ? `${lines} · ${formatEuro(entry.totalNet)}` : lines;
}

function renderPendingOrderCard(entry) {
  const answering = state.history.answering === entry.orderId;
  const busy = Boolean(state.history.answering);
  return `
    <article class="pending-order">
      <div class="pending-order__text">
        <strong class="pending-order__question">${escapeHtml(pendingOrderQuestion(entry))}</strong>
        <span class="pending-order__meta">${escapeHtml(pendingOrderMeta(entry))}</span>
      </div>
      <div class="pending-order__actions">
        <button type="button" class="button button--success button--wide" data-action="history-received" data-order-id="${escapeHtml(entry.orderId)}" ${busy ? "disabled" : ""}>
          ${answering ? "Attendere…" : "Sì, ricevuta"}
        </button>
        <button type="button" class="button button--secondary button--wide" data-action="history-not-received" data-order-id="${escapeHtml(entry.orderId)}" ${busy ? "disabled" : ""}>
          No, non ancora
        </button>
        <button type="button" class="button button--ghost button--wide" data-action="history-never" data-order-id="${escapeHtml(entry.orderId)}" ${busy ? "disabled" : ""}>
          Annullato, non arriva
        </button>
      </div>
      <p class="pending-order__note">Chi annulli sparisce dall’elenco e non ti viene più chiesto.</p>
    </article>`;
}

function renderPendingOrdersPanel() {
  const pending = visiblePendingOrders();
  if (state.history.errore) {
    return `
      <section class="pending-orders pending-orders--failed" role="alert" aria-label="Ordini non ancora ricevuti">
        <span class="pending-orders__label">Ordini non ancora ricevuti</span>
        <p class="pending-orders__note">${escapeHtml(state.history.errore)} Controlla tu quali ordini sono già partiti prima di ordinare di nuovo.</p>
      </section>`;
  }
  if (!pending.length) return "";
  return `
    <section class="pending-orders" role="region" aria-label="Ordini non ancora ricevuti">
      <span class="pending-orders__label">Ordini non ancora ricevuti</span>
      <p class="pending-orders__note">Rispondi qui sotto: così non riordini merce che hai già ordinato e che deve ancora arrivare.</p>
      ${pending.map(renderPendingOrderCard).join("")}
    </section>`;
}

function pendingOrdersFor(product) {
  return asArray(product?.pendingOrders);
}

function pendingOrderNoticeText(product, entry) {
  const quantity = Math.max(0, Math.trunc(finiteNumber(entry.quantity, 0)));
  // Uses the unit the order was placed in, not today's: the same barcode can
  // be a display one week and a regular product the next.
  const label = String(entry.unit || product.orderUnitLabel || "colli");
  const unit = quantity === 1 ? orderUnitSingular(label) : label;
  const supplier = pendingOrderSupplierLabel(entry);
  const day = formatDayMonth(entry.orderedAt);
  const quantityText = quantity > 0 ? `${formatInteger(quantity)} ${unit} da ${supplier}` : `merce da ${supplier}`;
  const closing = quantity === 1 ? "non ancora ricevuto" : "non ancora ricevuti";
  // A barcode shared by several items: attributing the pending quantity to
  // THIS item would show "20 already ordered" even for one that has none.
  // The quantity belongs to the order on that barcode, not to any one item,
  // and the sentence says so.
  if (entry.sharedWith > 1) {
    const base = day ? `${quantityText} il ${day}, ${closing}` : `${quantityText}, ${closing}`;
    return `Già ordinato sullo stesso codice a barre: ${base}. Il codice è condiviso da ${formatInteger(entry.sharedWith)} articoli del confronto: la quantità non è attribuibile a questo soltanto.`;
  }
  return day
    ? `Già ordinato: ${quantityText} il ${day}, ${closing}.`
    : `Già ordinato: ${quantityText}, ${closing}.`;
}

// Informational only: doesn't block compilation and doesn't change the order.
function renderPendingOrderNotice(product) {
  const entries = pendingOrdersFor(product);
  if (!entries.length) return "";
  return `
    <div class="pending-order-notice">
      ${entries.map((entry) => `<span>${escapeHtml(pendingOrderNoticeText(product, entry))}</span>`).join("")}
    </div>`;
}

// --- Promotions panel at the top of page 2 ---------------------------------
// Purely informational: doesn't touch prices, totals or the selected
// supplier. Reuses `promotionText()`/`renderBadge()`, already used by
// `renderPromotionSummary()` on page 3, instead of duplicating their
// formatting logic.

const PROMOTION_HIGHLIGHT_STATUSES = ["vicina", "ottenuta"];
const PROMOTION_HIGHLIGHT_LIMIT = 6;
const PROMOTION_STATUS_ORDER = { vicina: 0, ottenuta: 1, non_raggiunta: 2, da_verificare: 3 };

// --- Phase 8: from a list to a decision -------------------------------------
// On a real set of price lists, out of 188 recognized promotions 175 turn out
// to be discounts already folded into the per-piece price that decides which
// supplier wins: there's nothing to read, they already are the price. Mixing
// those in with the few that actually change a decision would make this panel
// unmanageable, so it's split into "actionable" and "already in price" instead.

function promotionIsAlreadyInPrice(promotion) {
  return Boolean(promotion?.economic_effect?.already_applied);
}

// Actionable: not already in the price, AND matched to at least one product.
// A promotion with no matched products doesn't say what it needs or what it
// applies to — it stays visible, but doesn't count among the ones that can
// change the order.
function promotionIsActionable(promotion) {
  return !promotionIsAlreadyInPrice(promotion) && promotionMatchedProductIds(promotion).length > 0;
}

function promotionRemainingQty(promotion) {
  const remaining = Number(promotion?.state?.remaining_qty);
  return Number.isFinite(remaining) ? remaining : Number.POSITIVE_INFINITY;
}

function promotionMatchesQuery(promotion, query) {
  const parole = String(query || "").toLocaleLowerCase("it").split(/\s+/).filter(Boolean);
  if (!parole.length) return true;
  const testo = [
    promotion?.source_text,
    promotionText(promotion),
    supplierName(promotion?.supplier),
    promotionKindLabel(promotion?.kind),
  ].join(" ").toLocaleLowerCase("it");
  return parole.every((parola) => testo.includes(parola));
}

// Closest-to-reach first: that's the order they're needed in, since a
// distant threshold doesn't change any decision made today.
function sortPromotionsByReach(items) {
  return items.slice().sort((a, b) => {
    const stato = (PROMOTION_STATUS_ORDER[a.state?.status] ?? 4) - (PROMOTION_STATUS_ORDER[b.state?.status] ?? 4);
    if (stato !== 0) return stato;
    const manca = promotionRemainingQty(a) - promotionRemainingQty(b);
    if (Number.isFinite(manca) && manca !== 0) return manca;
    return String(a.id).localeCompare(String(b.id));
  });
}

function promotionSupplierCounts(items) {
  const lette = items.length;
  const nelPrezzo = items.filter(promotionIsAlreadyInPrice).length;
  const azionabili = items.filter(promotionIsActionable).length;
  return { lette, nelPrezzo, azionabili, scollegate: lette - nelPrezzo - azionabili };
}

// The count line has to tell the truth: how many were read, and how many can
// actually change this order. A raw total by itself overstates the amount of
// work there is to review.
function promotionCountsLabel(grouped, supplierIds) {
  return supplierIds.map((id) => {
    const c = promotionSupplierCounts(grouped.get(id) || []);
    const nome = supplierName(id).toLocaleUpperCase("it");
    if (c.azionabili) return `${nome}: ${formatInteger(c.lette)} lette, ${formatInteger(c.azionabili)} utili`;
    if (c.scollegate) return `${nome}: ${formatInteger(c.lette)} lette, nessuna collegata ai tuoi prodotti`;
    return `${nome}: ${formatInteger(c.lette)} lette, già nel prezzo`;
  }).join(" · ");
}

function groupPromotionsBySupplier(promotions) {
  const grouped = new Map();
  for (const promotion of promotions) {
    const key = String(promotion?.supplier || "");
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(promotion);
  }
  return grouped;
}

function orderedPromotionSupplierIds(grouped) {
  const known = (state.review?.suppliers || []).map((supplier) => supplier.id).filter((id) => grouped.has(id));
  const extra = [...grouped.keys()].filter((id) => !known.includes(id)).sort((a, b) => a.localeCompare(b, "it"));
  return [...known, ...extra];
}

function renderPromotionHighlightItem(promotion) {
  const matchCount = promotionMatchedProductIds(promotion).length;
  const tone = promotion.state?.status === "ottenuta" ? "is-earned" : "is-near";
  return `
    <div class="promotion-highlight ${tone}">
      <span class="promotion-highlight__supplier">${escapeHtml(supplierName(promotion.supplier).toLocaleUpperCase("it"))}</span>
      <span class="promotion-highlight__text">${escapeHtml(promotionText(promotion))}</span>
      ${matchCount ? `<button type="button" class="button button--secondary promotion-highlight__action" data-action="filter-promotion" data-promotion-id="${escapeHtml(promotion.id)}">Mostra i prodotti</button>` : ""}
    </div>`;
}

function renderPromotionHighlights(promotions) {
  const highlighted = promotions
    .filter((promotion) => PROMOTION_HIGHLIGHT_STATUSES.includes(String(promotion.state?.status || "")))
    .sort((a, b) => (PROMOTION_STATUS_ORDER[a.state?.status] ?? 4) - (PROMOTION_STATUS_ORDER[b.state?.status] ?? 4));
  if (!highlighted.length) return "";
  const visible = highlighted.slice(0, PROMOTION_HIGHLIGHT_LIMIT);
  const remaining = highlighted.length - visible.length;
  return `
    <div class="promotion-highlights" role="status">
      <span class="promotion-highlights__label">Offerte a portata di mano</span>
      <div class="promotion-highlights__list">
        ${visible.map(renderPromotionHighlightItem).join("")}
      </div>
      ${remaining > 0 ? `<p class="promotion-highlights__more">+${formatInteger(remaining)} ${remaining === 1 ? "altra offerta vicina" : "altre offerte vicine"}: apri “Tutte le offerte dei fornitori” qui sotto.</p>` : ""}
    </div>`;
}

function renderPromotionCatalogItem(promotion) {
  const badge = promotionStatusBadge(promotion.state?.status);
  const matchCount = promotionMatchedProductIds(promotion).length;
  const sourceText = String(promotion.source_text || "").trim();
  return `
    <li class="promotion-catalog__item">
      <div class="promotion-catalog__item-top">
        <span class="promotion-catalog__kind">${escapeHtml(promotionKindLabel(promotion.kind))}</span>
        ${renderBadge(badge.label, badge.tone)}
      </div>
      ${sourceText ? `<p class="promotion-catalog__source">«${escapeHtml(sourceText)}»</p>` : ""}
      <p class="promotion-catalog__message">${escapeHtml(promotionText(promotion))}</p>
      <div class="promotion-catalog__item-bottom">
        <span>${matchCount ? `${formatInteger(matchCount)} ${matchCount === 1 ? "prodotto coinvolto" : "prodotti coinvolti"}` : "Nessun prodotto abbinato nell’ordine corrente"}</span>
        ${matchCount ? `<button type="button" class="button button--secondary" data-action="filter-promotion" data-promotion-id="${escapeHtml(promotion.id)}">Mostra i prodotti</button>` : ""}
      </div>
    </li>`;
}

function renderPromotionSupplierGroup(supplierId, items, titolo) {
  if (!items.length) return "";
  return `
    <div class="promotion-catalog__group">
      <h4>${escapeHtml(supplierName(supplierId).toLocaleUpperCase("it"))} · ${escapeHtml(titolo)}</h4>
      <ul class="promotion-catalog__items">
        ${sortPromotionsByReach(items).map(renderPromotionCatalogItem).join("")}
      </ul>
    </div>`;
}

function renderPromotionSearch(totale) {
  // Below a certain count the list is short enough to just read; the search
  // field would only be there to be looked at.
  if (totale < 12) return "";
  return `
    <div class="promotion-catalog__search">
      <label for="promotion-search">Cerca nel testo delle offerte</label>
      <input id="promotion-search" class="input" type="search" value="${escapeHtml(state.filters.promotionQuery)}"
        placeholder="Per esempio: omaggio, cartoni, Neval" data-promotion-search data-focus-key="promotion-search">
      ${state.filters.promotionQuery ? '<button type="button" class="button button--secondary" data-action="clear-promotion-query">Cancella la ricerca</button>' : ""}
    </div>`;
}

function renderPromotionCatalog(promotions) {
  const query = String(state.filters.promotionQuery || "").trim();
  const trovate = promotions.filter((promotion) => promotionMatchesQuery(promotion, query));
  const daDecidere = trovate.filter((promotion) => !promotionIsAlreadyInPrice(promotion));
  const nelPrezzo = trovate.filter(promotionIsAlreadyInPrice);

  const gruppiTutti = groupPromotionsBySupplier(promotions);
  const countsLabel = promotionCountsLabel(gruppiTutti, orderedPromotionSupplierIds(gruppiTutti));

  const gruppiDaDecidere = groupPromotionsBySupplier(daDecidere);
  const gruppiNelPrezzo = groupPromotionsBySupplier(nelPrezzo);

  const corpoDaDecidere = orderedPromotionSupplierIds(gruppiDaDecidere)
    .map((id) => {
      const items = gruppiDaDecidere.get(id) || [];
      const utili = items.filter(promotionIsActionable).length;
      const etichetta = utili
        ? `${formatInteger(utili)} ${utili === 1 ? "collegata ai tuoi prodotti" : "collegate ai tuoi prodotti"}${items.length > utili ? `, ${formatInteger(items.length - utili)} no` : ""}`
        : `${formatInteger(items.length)} ${items.length === 1 ? "condizione" : "condizioni"}, nessuna collegata ai tuoi prodotti`;
      return renderPromotionSupplierGroup(id, items, etichetta);
    })
    .join("");

  const corpoNelPrezzo = orderedPromotionSupplierIds(gruppiNelPrezzo)
    .map((id) => {
      const items = gruppiNelPrezzo.get(id) || [];
      return renderPromotionSupplierGroup(id, items, `${formatInteger(items.length)} ${items.length === 1 ? "condizione" : "condizioni"}`);
    })
    .join("");

  return `
    <details class="promotion-catalog" data-promotion-catalog ${state.promotionCatalogOpen ? "open" : ""}>
      <summary>
        <h3>Offerte dei fornitori</h3>
        <span class="promotion-catalog__counts">${escapeHtml(countsLabel)}</span>
      </summary>
      <div class="promotion-catalog__body">
        ${renderPromotionSearch(promotions.length)}
        ${query && !trovate.length ? `<p class="promotion-catalog__empty">Nessuna offerta contiene «${escapeHtml(query)}».</p>` : ""}
        ${corpoDaDecidere}
        ${nelPrezzo.length ? `
          <details class="promotion-catalog__included" ${apribile("offerte-nel-prezzo")}>
            <summary>Già conteggiate nel prezzo · ${formatInteger(nelPrezzo.length)} ${nelPrezzo.length === 1 ? "condizione" : "condizioni"}</summary>
            <p class="promotion-catalog__included-note">Sono sconti che il listino applica già al prezzo su cui scegli il fornitore. Restano qui per il controllo: non c'è niente da fare per ottenerli.</p>
            ${corpoNelPrezzo}
          </details>` : ""}
      </div>
    </details>`;
}

function renderPromotionPanel() {
  const promotions = asArray(state.review?.promotions);
  if (!promotions.length) return "";
  return `
    <div class="promotion-panel">
      ${renderPromotionHighlights(promotions)}
      ${renderPromotionCatalog(promotions)}
    </div>`;
}

function renderPromotionFilterChip() {
  if (!state.filters.promotionId) return "";
  const promotion = findPromotion(state.filters.promotionId);
  if (!promotion) return "";
  return `
    <div class="promotion-filter-chip" role="status">
      <span>Mostro solo i prodotti dell’offerta <strong>${escapeHtml(supplierName(promotion.supplier).toLocaleUpperCase("it"))}</strong> · ${escapeHtml(promotionText(promotion))}</span>
      <button type="button" class="button button--secondary" data-action="clear-promotion-filter">Rimuovi filtro</button>
    </div>`;
}

function renderCompactProduct(product) {
  const offer = selectedOffer(product);
  const issues = issuesForProduct(product);
  const blocker = issues.some((issue) => issue.blocking);
  const excluded = isExcluded(product);
  const calculation = offer ? offerCalculation(product, offer) : null;
  const factorChangeNotice = state.supplierChangeNotices.get(product.id) || null;
  return `
    <article class="product-card${blocker ? " has-blocker" : issues.length ? " has-warning" : ""}${excluded ? " is-excluded" : ""}${factorChangeNotice ? " is-supplier-changed" : ""}">
      <div class="product-card__header">
        <div class="product-main">
          <div class="product-main__title">
            <h3 title="${escapeHtml(product.name)}">${escapeHtml(product.name)}</h3>
            ${renderBadge(productTypeLabel(product), productTypeTone(product))}
            ${/* The breakdown line says how many came from where, but not which ones:
                 manually added items had no marker anywhere on the card, so
                 finding them again meant remembering what had been added. A
                 badge here, and a "Show" filter entry, shown only when at
                 least one exists. */ ""}
            ${product.addedManually ? renderBadge("Aggiunto a mano", "info") : ""}
            ${excluded ? renderBadge("Escluso", "neutral") : ""}
            ${/* Blocking vs. warning is stated in words, not just a thin color strip
                 on the card's edge, which would be close to indistinguishable
                 for red-green color blindness or on a low-quality monitor,
                 and slow to spot when scanning many cards. A count of issues
                 doesn't say whether one of them is blocking either. An
                 excluded product has no issues — `collectIssues` skips
                 zero-quantity items — so this badge and "Excluded" never
                 appear together. */ ""}
            ${blocker ? renderBadge("Da sistemare", "danger") : issues.length ? renderBadge("Da controllare", "warning") : ""}
          </div>
          <div class="product-main__meta">
            <span>${product.ean ? `EAN ${escapeHtml(product.ean)}` : "Senza EAN padre"}</span>
            ${/* "offerta" in Italian retail means a discount, so a count of
                 "offers" would read as a count of promotions, not suppliers.
                 Counts distinct suppliers, not rows, so the number stays
                 correct even if one price list lists the same item twice. */ ""}
            <span>${escapeHtml(contati(new Set(product.offers.filter((candidate) => candidate.available).map((candidate) => candidate.supplierId)).size, "fornitore ce l’ha", "fornitori ce l’hanno"))}</span>
            ${/* "0 suppliers have it" on a product rejected everywhere would
                 be the same false claim the offers panel avoids elsewhere:
                 those suppliers do have the row, the user said it's a
                 different item. The "available" count stays about what can be
                 ordered; this count separately says how many were excluded by
                 the user's own answers. */ ""}
            ${rifiutatiDaTe(product) ? `<span>${escapeHtml(contati(rifiutatiDaTe(product), "rifiutato da te", "rifiutati da te"))}</span>` : ""}
            ${issues.length ? `<span>${escapeHtml(contati(issues.length, "controllo", "controlli"))}</span>` : ""}
            ${product.lastUnitPrice != null ? `<span>Ultimo pagato: ${formatEuro(product.lastUnitPrice)}/pz</span>` : ""}
          </div>
        </div>
        <button class="button button--danger-soft" type="button" data-action="${excluded ? "restore-product" : "exclude-product"}" data-product-id="${escapeHtml(product.id)}">
          ${excluded ? "Rimetti nell’ordine" : "Escludi dall’ordine"}
        </button>
      </div>

      ${excluded ? `
        <div class="excluded-message">Questo prodotto non sarà inserito negli ordini. Puoi ripristinarlo in qualsiasi momento.</div>
      ` : `
        ${factorChangeNotice ? `<div class="supplier-change-notice">Attenzione: con ${escapeHtml(factorChangeNotice.supplierName)} ${escapeHtml(factorChangeNotice.quantityText)} sono ${formatInteger(factorChangeNotice.newPieces)} pezzi invece di ${formatInteger(factorChangeNotice.previousPieces)}.</div>` : ""}
        ${renderPendingOrderNotice(product)}
        <div class="product-order-row">
          ${renderQuantityControl(product)}
          <div class="selected-choice">
            <span>Fornitore scelto</span>
            <strong>${offer ? escapeHtml(offer.supplierName) : "Da scegliere"}</strong>
            ${offer?.ean ? `<small>EAN ${escapeHtml(offer.ean)}</small>` : ""}
          </div>
          <div class="price-summary">
            <span>Totale per questa quantità</span>
            <strong>${calculation && calculation.desired > 0 ? formatEuro(calculation.total) : "—"}</strong>
            <small>${calculation ? `${formatEuro(calculation.pricePerPiece)} per pezzo` : ""}</small>
          </div>
        </div>
        <div class="product-comparison">
          ${/* No "Supplier comparison" heading here: it would repeat on every
               card, above a table whose first column is already labeled
               "Supplier". */ ""}
          ${renderProductIssues(product)}
          ${renderCandidateDecisions(product)}
          ${renderOfferGrid(product)}
          ${renderConfirmation(product)}
          ${renderComponents(product)}
        </div>
      `}
    </article>`;
}

// Where the products being viewed come from. The total alone looks like a
// stale count from an old list unless something breaks it down.
function composizioneDellElenco() {
  const prodotti = state.review.products;
  const aggiunti = prodotti.filter((prodotto) => prodotto.addedManually).length;
  const espositori = prodotti.filter((prodotto) => prodotto.itemType === "display").length;
  const dallElenco = prodotti.length - aggiunti - espositori;
  const pezzi = [`${formatInteger(dallElenco)} dall’elenco del gestionale`];
  // Says where to find them, not just how many: naming a count without a
  // location left the user to remember what they'd added.
  if (espositori) {
    pezzi.push(`${formatInteger(espositori)} ${espositori === 1 ? "espositore dei listini" : "espositori dei listini"} (filtro «Tipo di prodotto»)`);
  }
  if (aggiunti) {
    pezzi.push(`${formatInteger(aggiunti)} ${aggiunti === 1 ? "aggiunto" : "aggiunti"} a mano (filtro «Mostra»)`);
  }
  return pezzi.join(", ");
}

// An excluded product doesn't appear under any filter except "Excluded": a
// search by name would otherwise report "no product found" and the user
// would conclude it isn't in the list, when it's simply excluded.
function esclusiCheCorrispondono() {
  const search = state.filters.search.trim().toLocaleLowerCase("it");
  if (!search || state.filters.status === "excluded") return [];
  return state.review.products.filter((prodotto) => {
    if (!isExcluded(prodotto)) return false;
    const testo = `${prodotto.name} ${prodotto.ean} ${prodotto.components.map((componente) => componente.name).join(" ")}`;
    return testo.toLocaleLowerCase("it").includes(search);
  });
}

function nascostiPerchePropriEsclusi(esclusi) {
  if (!esclusi.length) return "";
  const nomi = esclusi.slice(0, 3).map((prodotto) => prodotto.name).join(", ");
  const coda = esclusi.length > 3 ? ` e altri ${formatInteger(esclusi.length - 3)}` : "";
  return renderAlert({
    title: esclusi.length === 1 ? "Un prodotto che cerchi è fra gli esclusi" : `${formatInteger(esclusi.length)} prodotti che cerchi sono fra gli esclusi`,
    message: `${escapeHtml(nomi)}${coda}: ${esclusi.length === 1 ? "è nell’elenco" : "sono nell’elenco"} ma ${esclusi.length === 1 ? "l’hai messo" : "li hai messi"} fra i prodotti da non ordinare, e i filtri ${esclusi.length === 1 ? "lo nascondono" : "li nascondono"}. ${esclusi.length === 1 ? "Lo ritrovi" : "Li ritrovi"} con «Mostra» → «Esclusi», dove c’è il pulsante «Rimetti nell’ordine».`,
    severity: "warning",
    blocking: false,
  });
}

function renderQuantityStep() {
  const matchingProducts = filteredProducts();
  const totalPages = Math.max(1, Math.ceil(matchingProducts.length / state.filters.pageSize));
  state.filters.page = Math.min(totalPages, Math.max(1, state.filters.page));
  const start = (state.filters.page - 1) * state.filters.pageSize;
  const products = matchingProducts.slice(start, start + state.filters.pageSize);
  const ordered = orderedProducts().length;
  return `
    ${/* The note about supplier ordering lives here, once per page, instead
         of under every grid. */ ""}
    ${pageHeading("2. Scegli prodotti e fornitori", "Indica i colli da ordinare per ogni prodotto o kit. Per gli espositori indica il numero di espositori completi. I fornitori di ogni prodotto sono in ordine, dal più conveniente al pezzo.")}
    ${renderStickySupplierTotals()}
    ${renderCambiamentoDocumenti()}
    ${renderSourceWarnings(2)}
    ${renderSupplierReassignments()}
    ${renderPendingOrdersPanel()}
    ${renderPromotionPanel()}
    ${state.azzeramentoUndo ? `
      <div class="undo-bar" role="status">
        <span>${escapeHtml(contati(state.azzeramentoUndo.prodotti.length, "quantità proposta dal gestionale azzerata", "quantità proposte dal gestionale azzerate"))}.</span>
        <button type="button" class="button button--secondary" data-action="undo-azzeramento">Rimetti le quantità</button>
      </div>` : ""}
    ${state.exclusionUndo ? `
      <div class="undo-bar" role="status">
        <span><strong>${escapeHtml(state.exclusionUndo.name)}</strong> è stato escluso dall’ordine.</span>
        <button type="button" class="button button--secondary" data-action="undo-exclude">Rimetti nell’ordine</button>
      </div>` : ""}
    <div class="panel__header">
      <div><h3>${ordered} ${ordered === 1 ? "prodotto da ordinare" : "prodotti da ordinare"}</h3><p>${matchingProducts.length} ${matchingProducts.length === 1 ? "prodotto trovato" : "prodotti trovati"} su ${state.review.products.length} (${composizioneDellElenco()})${totalPages > 1 ? ` · pagina ${state.filters.page} di ${totalPages}` : ""}</p></div>
      <div class="button-row">
        <button class="button button--secondary" type="button" data-action="open-catalog" data-focus-key="apri-catalogo">+ Aggiungi un prodotto</button>
      </div>
    </div>
    ${renderToolbar()}
    ${renderPromotionFilterChip()}
    ${nascostiPerchePropriEsclusi(esclusiCheCorrispondono())}
    <div class="product-list">
      ${products.length ? products.map(renderCompactProduct).join("") : '<div class="empty-state"><div><strong>Nessun prodotto trovato</strong><p>Modifica ricerca o filtri per continuare.</p></div></div>'}
    </div>
    ${totalPages > 1 ? `
      <nav class="product-pagination" aria-label="Pagine dei prodotti">
        <button class="button button--secondary" type="button" data-action="products-previous-page" ${state.filters.page <= 1 ? "disabled" : ""}>← Pagina precedente</button>
        <strong>Pagina ${state.filters.page} di ${totalPages}</strong>
        <button class="button button--secondary" type="button" data-action="products-next-page" ${state.filters.page >= totalPages ? "disabled" : ""}>Pagina successiva →</button>
      </nav>` : ""}
    <div class="page-actions">
      <button class="button button--secondary" type="button" data-action="previous">← Torna all’importazione</button>
      <button class="button button--primary" type="button" data-action="next">Vai al riepilogo →</button>
    </div>
    ${renderCatalogDialog()}
    ${renderListinoDialog()}`;
}

function normalizeCatalogProduct(product, index = 0) {
  const suppliers = asArray(product.suppliers ?? product.availableSuppliers ?? product.available_suppliers);
  return {
    id: String(product.catalogId ?? product.catalog_id ?? product.id ?? product.productId ?? product.product_id ?? product.sourceId ?? `catalog-${index + 1}`),
    name: String(product.name ?? product.description ?? `Prodotto ${index + 1}`),
    ean: String(product.ean ?? product.barcode ?? ""),
    itemType: String(product.itemType ?? product.item_type ?? "product"),
    supplierName: String(product.supplierName ?? product.supplier_name ?? suppliers.map((supplier) => supplier.name ?? supplier).filter(Boolean).join(", ") ?? ""),
    suppliersCount: Math.max(0, finiteNumber(product.suppliersCount ?? product.suppliers_count ?? suppliers.length, 0)),
    bestPrice: Math.max(0, finiteNumber(product.bestPrice ?? product.best_price ?? product.price, 0)),
    sourceId: String(product.sourceId ?? product.source_id ?? product.id ?? ""),
    alreadyPresent: Boolean(product.alreadyPresent ?? product.already_present),
  };
}

function demoCatalogProducts(query) {
  const products = [
    { id: "extra-demo-1", name: "Neval Sun Latte Protect & Hydrate SPF 30", ean: "4009822228322", suppliersCount: 3, bestPrice: 6.42 },
    { id: "extra-demo-2", name: "Solbao Spray Bimbi SPF 50+", ean: "8009436285359", suppliersCount: 2, bestPrice: 7.18 },
  ].map(normalizeCatalogProduct);
  const needle = query.trim().toLocaleLowerCase("it");
  return needle ? products.filter((product) => `${product.name} ${product.ean}`.toLocaleLowerCase("it").includes(needle)) : products;
}

// --- The price-list viewer --------------------------------------------------
//
// Why it exists: the same item can sit under different barcodes on different
// suppliers' price lists, with a variant detail the management export's
// product name doesn't carry at all. No matching heuristic can infer that;
// a person with the price list open sees it in a second.
//
// Shows the price list as the app read it, not the original spreadsheet:
// if a column was parsed wrong, it shows wrong here too, which is exactly the
// information needed to diagnose it. Rows the app discarded are always
// counted, even when none are shown.

function apriIlListino(prodottoId, fornitore = "", riga = null) {
  ricordaChiApreLaFinestra();
  state.listino = {
    ...state.listino,
    aperto: true,
    prodottoId: String(prodottoId || ""),
    fornitore: String(fornitore || ""),
    // `fornitori` is deliberately not reset here: an already-loaded
    // supplier list survives even if the next request never comes back
    // (service down, network drop), keeping the dropdown usable.
    nome: "",
    query: "",
    da: 0,
    rigaFuoco: riga,
    righe: [],
    errore: "",
    problema: "",
    esito: "",
  };
  render();
  portaIlFuocoDentro(["#listino-cerca", ".listino-dialog .dialog-close"]);
  caricaIlListino({ riga });
}

function chiudiIlListino() {
  if (state.listino.timer) clearTimeout(state.listino.timer);
  state.listino = { ...state.listino, aperto: false, timer: null, righe: [], esito: "" };
  render();
  restituisciIlFuoco();
}

// --- Which column the order gets written to --------------------------------
// This can be changed at any time, and takes effect immediately: the local
// service rebuilds the write configuration as soon as it accepts the choice.

async function apriLaColonnaDOrdine(fornitore) {
  ricordaChiApreLaFinestra();
  state.colonnaOrdine = {
    ...state.colonnaOrdine,
    aperta: true,
    fornitore: String(fornitore || ""),
    nome: supplierName(fornitore) || String(fornitore || ""),
    caricando: true,
    salvando: false,
    errore: "",
    colonne: [],
    attuale: null,
    scelta: "",
  };
  render();
  // Focus is moved once, now: the column dropdown doesn't exist yet, so focus
  // lands on the dialog's close button. Moving it again once the response
  // arrives would yank it away from a user who has already tabbed elsewhere.
  portaIlFuocoDentro(["#colonna-ordine-scelta", ".colonna-dialog .dialog-close"]);
  const richiesto = String(fornitore || "");
  try {
    const payload = await requestJson(`${API.colonnaOrdine}?fornitore=${encodeURIComponent(fornitore)}`);
    // It's not enough that a dialog is open: it must still be open for THIS
    // supplier. Opening the dialog for one supplier, closing it before the
    // response arrives, then opening it for another would let the first
    // response land in state that now describes the second supplier, and
    // "Write the order here" would then save the second supplier's column
    // number against the first supplier's document.
    if (!state.colonnaOrdine.aperta || state.colonnaOrdine.fornitore !== richiesto) return;
    const colonne = asArray(payload.colonne);
    const attuale = payload.attuale || null;
    state.colonnaOrdine = {
      ...state.colonnaOrdine,
      nome: String(payload.supplierName || state.colonnaOrdine.nome),
      fileName: String(payload.fileName || ""),
      foglio: String(payload.sheet || ""),
      colonne,
      attuale,
      // Starts from the column already in use: the dialog opens on the
      // current state, not on an already-changed choice a user could confirm
      // by mistake.
      scelta: attuale && attuale.colonna ? String(attuale.colonna) : "",
      caricando: false,
    };
  } catch (error) {
    if (!state.colonnaOrdine.aperta || state.colonnaOrdine.fornitore !== richiesto) return;
    state.colonnaOrdine = {
      ...state.colonnaOrdine,
      caricando: false,
      errore: error.message || "Non sono riuscito a leggere le colonne del documento.",
    };
  }
  render();
}

function chiudiLaColonnaDOrdine() {
  state.colonnaOrdine = { ...state.colonnaOrdine, aperta: false, colonne: [], errore: "", scelta: "" };
  render();
  restituisciIlFuoco();
}

async function salvaLaColonnaDOrdine() {
  const finestra = state.colonnaOrdine;
  const scelta = colonnaOrdineScelta();
  if (finestra.salvando || !scelta) return;
  state.colonnaOrdine = { ...state.colonnaOrdine, salvando: true, errore: "" };
  render();
  try {
    const esito = await requestJson(API.colonnaOrdine, {
      method: "POST",
      body: JSON.stringify({ supplierId: finestra.fornitore, colonna: scelta.colonna }),
    });
    state.colonnaOrdine = { ...state.colonnaOrdine, aperta: false, salvando: false, colonne: [] };
    // The dialog closes by writing state directly rather than through
    // `chiudiLaColonnaDOrdine()`, so focus is restored here explicitly —
    // otherwise a keyboard user saving with Enter would lose their focus.
    restituisciIlFuoco();
    // A partial success isn't reported as a success: the column changed, but
    // if the write configuration wasn't rebuilt, that needs a warning tone,
    // not a success one.
    showToast(esito?.message || "Colonna cambiata.", esito?.avviso ? "error" : "success");
    // The page caches the documents' columns for the run; without
    // invalidating them here, the card would keep showing the old column.
    state.colonneDocumenti.caricate = false;
    state.colonneDocumenti.runId = "";
    await caricaColonneDeiDocumenti();
    render();
  } catch (error) {
    state.colonnaOrdine = {
      ...state.colonnaOrdine,
      salvando: false,
      errore: error.message || "La colonna non è stata cambiata.",
    };
    render();
  }
}

async function caricaIlListino({ riga = null } = {}) {
  if (mode === "demo") return;
  // The last response to arrive isn't necessarily the right one: fast
  // repeated reads (double-clicking "next", typing while a read is in
  // flight) can complete out of order. Without a request number, the page
  // would apply whatever came back last, potentially showing rows from a
  // search the user has already moved past. This table is where "It's this
  // one" writes a permanent match to `conferme.db`, so a stale row here is
  // the costliest place in the app to get it wrong.
  const mia = (state.listino.richiesta || 0) + 1;
  state.listino.richiesta = mia;
  state.listino.caricando = true;
  state.listino.errore = "";
  state.listino.problema = "";
  rerenderPreservingFocus();
  try {
    const parametri = new URLSearchParams({
      fornitore: state.listino.fornitore,
      q: state.listino.query,
      da: String(state.listino.da),
      quante: String(state.listino.quante),
    });
    if (riga !== null && riga !== undefined) parametri.set("riga", String(riga));
    const payload = await requestJson(`${API.listino}?${parametri.toString()}`);
    if (state.listino.richiesta !== mia) return;
    state.listino = {
      ...state.listino,
      caricando: false,
      fornitore: String(payload.supplier || state.listino.fornitore),
      // The name comes from the adapter registry, already read by the
      // service: when the requested price list can't be browsed, there's no
      // entry in `fornitori` to take it from, and the raw id would otherwise
      // end up in the title.
      nome: String(payload.supplierName || state.listino.nome || ""),
      fornitori: asArray(payload.fornitori),
      righe: asArray(payload.righe),
      da: Math.max(0, finiteNumber(payload.da, 0)),
      totale: Math.max(0, finiteNumber(payload.totale, 0)),
      trovate: Math.max(0, finiteNumber(payload.trovate, 0)),
      scartate: Math.max(0, finiteNumber(payload.scartate, 0)),
      rigaFuoco: payload.rigaCercata ?? state.listino.rigaFuoco,
      problema: String(payload.problema?.messaggio || ""),
    };
  } catch (error) {
    if (state.listino.richiesta !== mia) return;
    state.listino.caricando = false;
    state.listino.errore = error.message;
  }
  rerenderPreservingFocus();
}

// A manual match: the offer enters the comparison immediately, and the two
// barcodes are declared equivalent for future comparisons. The message
// returned by the service says which of the two effects actually happened —
// when either side has no barcode, the second isn't possible, and saying so
// is the only way not to promise something that would silently not survive
// the next comparison.
async function abbinaLaRiga(riga) {
  if (mode === "demo" || state.listino.abbinando) return;
  state.listino.abbinando = String(riga);
  state.listino.esito = "";
  rerenderPreservingFocus();
  try {
    // Save BEFORE the request, not after — same reason as `addCatalogProduct`
    // and its sibling routes: `loadReview()` below rereads the comparison
    // from the service and REPLACES what's in the page, and a quantity
    // written moments ago (or a save still pending retry) may not be on disk
    // yet. Rereading would discard it, and the next save would then persist
    // the stale number as the real order, silently. With clean state,
    // `saveState()` returns immediately.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const esito = await requestJson(API.matchAbbina, {
      method: "POST",
      body: JSON.stringify({
        // The run id, sent for the same reason the rejection and the
        // candidate answer already send it: `productId` and `sourceRow` are
        // positional — a row in the management file and a row in the price
        // list — so after a new comparison they can point at different
        // items. Without the run id, the service would match whichever two
        // items now sit at those positions and permanently remember their
        // barcodes as the same article — a risk any stale browser tab still
        // showing an old comparison would trigger.
        runId: state.review?.run?.id || "",
        productId: state.listino.prodottoId,
        supplierId: state.listino.fornitore,
        sourceRow: riga,
      }),
    });
    state.listino.esito = String(esito?.message || "Abbinato.");
    state.listino.rigaFuoco = String(riga);
    await loadReview();
  } catch (error) {
    state.listino.esito = "";
    state.listino.errore = error.message;
  } finally {
    state.listino.abbinando = "";
    rerenderPreservingFocus();
  }
}

// --- "These two barcodes are the same item" declarations -------------------
// Live under Settings, with names, supplier and search, rather than buried
// inside a single product's "browse the price lists" dialog: a wrong
// declaration affects every future comparison across every supplier, so it
// needs to be reviewable without a comparison open, and by name rather than
// by bare barcode — the name is the only thing that lets someone judge
// whether the declaration is correct.

async function caricaLeUguaglianze({ query = null } = {}) {
  if (mode === "demo") return;
  const magazzino = state.impostazioni.uguaglianze;
  if (query !== null) magazzino.query = String(query);
  magazzino.caricando = true;
  // Preserves focus: search happens while typing, and a plain `render()`
  // would drop the cursor out of the search field mid-word.
  rerenderPreservingFocus();
  try {
    const payload = await requestJson(
      `${API.uguaglianze}?q=${encodeURIComponent(magazzino.query || "")}`,
    );
    state.impostazioni.uguaglianze = {
      ...state.impostazioni.uguaglianze,
      caricate: true,
      caricando: false,
      voci: asArray(payload.uguaglianze),
      totale: Number(payload.totale) || 0,
      motivo: String(payload.motivo || ""),
      errore: "",
    };
  } catch (error) {
    state.impostazioni.uguaglianze = {
      ...state.impostazioni.uguaglianze,
      caricate: true,
      caricando: false,
      errore: error.message || "Non sono riuscito a leggere le dichiarazioni.",
    };
  }
  rerenderPreservingFocus();
}

function cercaFraLeUguaglianze(testo) {
  const magazzino = state.impostazioni.uguaglianze;
  magazzino.query = String(testo || "");
  if (magazzino.timer) clearTimeout(magazzino.timer);
  // Same debounce as the price-list search: search while typing, but not on
  // every keystroke.
  magazzino.timer = setTimeout(() => caricaLeUguaglianze(), 220);
}

async function togliLUguaglianza(codici) {
  const magazzino = state.impostazioni.uguaglianze;
  magazzino.togliendo = codici.join(",");
  render();
  try {
    const esito = await requestJson(API.uguaglianzaTogli, {
      method: "POST",
      body: JSON.stringify({ codici }),
    });
    showToast(esito?.message || "Dichiarazione tolta.", "success");
    magazzino.togliendo = "";
    // The updated list comes back with the response, so rereading it
    // separately would be wasted work. But it comes back unfiltered, so
    // an active search has to be re-requested — otherwise the list would
    // silently widen back out after a removal.
    if (magazzino.query.trim()) {
      await caricaLeUguaglianze();
      return;
    }
    state.impostazioni.uguaglianze = {
      ...magazzino,
      voci: asArray(esito?.uguaglianze),
      totale: asArray(esito?.uguaglianze).length,
    };
    render();
  } catch (error) {
    magazzino.togliendo = "";
    showToast(error.message || "La dichiarazione non è stata tolta.", "error");
    render();
  }
}

function renderSettingsUguaglianzePanel() {
  const magazzino = state.impostazioni.uguaglianze;
  const voci = asArray(magazzino.voci);
  const cercando = Boolean(String(magazzino.query || "").trim());
  return `
    <section class="settings-card">
      <h3>Codici dichiarati lo stesso articolo</h3>
      <p class="settings-card__intro">
        Quando abbini a mano una riga di listino a un prodotto, mi ricordo che i due codici a barre
        sono lo stesso articolo: da lì in poi si incontrano da soli, su <strong>tutti</strong> i fornitori.
        Una dichiarazione sbagliata è un ordine sbagliato ogni settimana — qui si rileggono e si tolgono.
      </p>
      ${magazzino.errore ? renderAlert({ title: "Dichiarazioni", message: magazzino.errore, severity: "error", blocking: false }) : ""}
      ${magazzino.motivo ? renderAlert({ title: "Dichiarazioni", message: magazzino.motivo, severity: "warning", blocking: false }) : ""}
      <div class="field">
        <label for="impostazioni-uguaglianze-cerca">Cerca fra le dichiarazioni</label>
        <input id="impostazioni-uguaglianze-cerca" class="input" type="search" value="${escapeHtml(magazzino.query || "")}"
               placeholder="Nome, codice a barre o fornitore" data-uguaglianze-cerca data-focus-key="impostazioni-uguaglianze-cerca">
      </div>
      ${magazzino.caricando ? '<div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Leggo le dichiarazioni…</span></div>' : ""}
      ${!magazzino.caricando && !voci.length ? `
        <div class="empty-state empty-state--small"><div>
          <strong>${cercando ? "Nessuna dichiarazione trovata" : "Nessuna dichiarazione"}</strong>
          <p>${cercando
            ? `Ne hai ${escapeHtml(contati(magazzino.totale, "dichiarata in tutto", "dichiarate in tutto"))}: prova con una parola più corta.`
            : "Ne nasce una ogni volta che abbini a mano una riga di listino a un prodotto, da «Sfoglia i listini»."}</p>
        </div></div>` : ""}
      ${voci.length ? `<ul class="uguaglianze">${voci.map(renderUguaglianza).join("")}</ul>` : ""}
    </section>`;
}

// Confirmations given — "yes, this price-list row is my product" — aren't
// readable anywhere else: the store is a SQLite file under `app/data/`,
// which is outside version control, so no copy of it exists elsewhere. The
// download link below is the only way to get one without copying an open
// database file by hand.
function renderSettingsConfermePanel() {
  return `
    <section class="settings-card">
      <h3>Le conferme che hai dato</h3>
      <p class="settings-card__intro">
        Ogni volta che confermi «sì, è lo stesso articolo», me lo ricordo per sempre: la conferma vale
        anche per il listino della settimana prossima. Quel ricordo vive in un file solo, su questo computer, e
        nessuno lo copia da nessuna parte: <strong>il file qui sotto è la tua copia</strong>. Scaricalo ogni tanto
        e mettilo altrove — è l’unico modo di non ricominciare da capo se il computer si rompe.
      </p>
      <p class="settings-card__intro">
        Il file che scarichi qui è anche l’unico modo di rileggerle tutte insieme, con la data e l’articolo su cui
        valgono. Le conferme sostituite o tolte ci sono anche loro: servono a rispondere a «che cosa avevo deciso
        prima, e quando».
      </p>
      <p>
        <a class="button button--secondary" href="${escapeHtml(API.confermeEsporta)}" download>
          Scarica tutto quello che hai confermato
        </a>
      </p>
    </section>`;
}

// A full declaration: what the management file asks for, what the supplier
// offers, and when it was made. Names shown are the normalized ones —
// uppercase, no punctuation — because that's what the matching logic
// actually compares; a prettier display name would show something other
// than what the declaration is defined over.
function renderUguaglianza(voce) {
  const codici = asArray(voce.codici);
  const chiave = codici.join(",");
  const togliendo = state.impostazioni.uguaglianze.togliendo === chiave;
  const gestionale = voce.gestionale || {};
  const listino = voce.listino || {};
  const giorno = formatDayMonth(voce.dal);
  return `
    <li class="uguaglianza">
      <div class="uguaglianza__lati">
        <div class="uguaglianza__lato">
          <span class="uguaglianza__chi">Gestionale</span>
          <strong>${escapeHtml(gestionale.nome || "nome non registrato")}</strong>
          <code>${escapeHtml(gestionale.codice || codici[0] || "")}</code>
        </div>
        <span class="uguaglianza__uguale" aria-hidden="true">=</span>
        <div class="uguaglianza__lato">
          <span class="uguaglianza__chi">${escapeHtml(listino.fornitore || "Listino")}</span>
          <strong>${escapeHtml(listino.nome || "nome non registrato")}</strong>
          <code>${escapeHtml(listino.codice || codici[1] || "")}</code>
          ${listino.codiceFornitore ? `<span class="uguaglianza__codice">cod. ${escapeHtml(listino.codiceFornitore)}</span>` : ""}
        </div>
      </div>
      <div class="uguaglianza__coda">
        <span>${escapeHtml(voce.motivo || "")}${giorno ? ` · ${escapeHtml(giorno)}` : ""}</span>
        <button type="button" class="button button--ghost button--piccolo" data-action="togli-uguaglianza"
          data-codici="${escapeHtml(chiave)}" ${togliendo ? "disabled" : ""}>
          ${togliendo ? "Tolgo…" : "Non sono lo stesso"}
        </button>
      </div>
    </li>`;
}

// What's inside a column, in one line. This is the deciding information: the
// app only refuses what it can prove is wrong (a column it already reads, a
// column of formulas), and leaves everything else for the user, who has the
// price list open, to judge. A supplier's order column can legitimately
// already hold thousands of section titles, so "the column must be empty"
// would reject a perfectly normal document.
function descriviColonna(colonna) {
  if (colonna.occupataDa) return `la leggo come ${colonna.occupataDa}`;
  if (colonna.formule) return `${contati(colonna.formule, "formula", "formule")} — l’ordine le cancellerebbe`;
  if (!colonna.valori) return "vuota";
  return `${contati(colonna.valori, "valore", "valori")}${colonna.esempio ? ` · es. ${colonna.esempio}` : ""}`;
}

function etichettaColonna(colonna) {
  const testa = colonna.intestazione ? `${colonna.lettera} — ${colonna.intestazione}` : `${colonna.lettera}`;
  return `${testa} · ${descriviColonna(colonna)}`;
}

function colonnaOrdineScelta() {
  const finestra = state.colonnaOrdine;
  return finestra.colonne.find((voce) => String(voce.colonna) === String(finestra.scelta)) || null;
}

function renderColonnaOrdineDialog() {
  const finestra = state.colonnaOrdine;
  if (!finestra.aperta) return "";
  const scelta = colonnaOrdineScelta();
  const attuale = finestra.attuale ? String(finestra.attuale.lettera || "") : "";
  // A column with content isn't rejected — it's disclosed. Whoever picks
  // their supplier's column already knows what's in it; whoever doesn't now
  // reads it before confirming.
  const avviso = scelta && !scelta.occupataDa && !scelta.formule && scelta.valori
    ? `La colonna ${scelta.lettera} non è vuota: contiene ${contati(scelta.valori, "valore", "valori")}. Le quantità ci finiscono sopra.`
    : "";
  return `
    <div class="dialog-backdrop" role="presentation">
      <section class="colonna-dialog" role="dialog" aria-modal="true" aria-labelledby="colonna-title">
        <div class="catalog-dialog__header">
          <div>
            <h2 id="colonna-title">In quale colonna scrivere l’ordine di ${escapeHtml(finestra.nome || finestra.fornitore)}</h2>
            <p>${finestra.fileName ? `${escapeHtml(finestra.fileName)}${finestra.foglio ? `, foglio «${escapeHtml(finestra.foglio)}»` : ""}. ` : ""}${attuale ? `Adesso è la <strong>${escapeHtml(attuale)}</strong>.` : ""}</p>
          </div>
          <button type="button" class="dialog-close" data-action="chiudi-colonna-ordine" aria-label="Chiudi la finestra">×</button>
        </div>
        ${finestra.caricando ? '<div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Leggo le colonne del documento…</span></div>' : ""}
        ${finestra.errore ? renderAlert({ title: "La colonna non è stata cambiata", message: finestra.errore, severity: "error", blocking: false }) : ""}
        ${!finestra.caricando && finestra.colonne.length ? `
          <div class="field">
            <label for="colonna-ordine-scelta">Colonna</label>
            <select id="colonna-ordine-scelta" class="select" data-colonna-ordine data-focus-key="colonna-ordine-scelta">
              ${finestra.colonne.map((colonna) => `
                <option value="${escapeHtml(String(colonna.colonna))}" ${String(colonna.colonna) === String(finestra.scelta) ? "selected" : ""} ${colonna.scegliibile ? "" : "disabled"}>
                  ${escapeHtml(etichettaColonna(colonna))}${colonna.attuale ? " · in uso" : ""}
                </option>`).join("")}
            </select>
          </div>
          ${avviso ? `<p class="colonna-dialog__avviso">${escapeHtml(avviso)}</p>` : ""}
          <p class="colonna-dialog__nota">Prima di scrivere controllo sul documento vero che quella colonna si possa riempire — la stessa verifica che faccio a ogni compilazione. Se non passa, non cambia niente e ti dice perché.</p>
        ` : ""}
        <div class="page-actions">
          <button type="button" class="button button--secondary" data-action="chiudi-colonna-ordine">Annulla</button>
          ${/* Retry button: on error this dialog otherwise had nothing to do
               but close and reopen it from the supplier's card. */ ""}
          ${finestra.errore ? `<button type="button" class="button button--secondary" data-action="apri-colonna-ordine" data-supplier-id="${escapeHtml(finestra.fornitore)}">Riprova</button>` : ""}
          <button type="button" class="button button--primary" data-action="salva-colonna-ordine"
            ${finestra.salvando || finestra.caricando || !scelta || !scelta.scegliibile || scelta.attuale ? "disabled" : ""}>
            ${finestra.salvando ? "Controllo il documento…" : "Scrivi l’ordine qui"}
          </button>
        </div>
      </section>
    </div>`;
}

function renderListinoDialog() {
  if (!state.listino.aperto) return "";
  const prodotto = findProduct(state.listino.prodottoId);
  const scelto = state.listino.fornitori.find((voce) => voce.id === state.listino.fornitore);
  return `
    <div class="dialog-backdrop" role="presentation">
      <section class="listino-dialog" role="dialog" aria-modal="true" aria-labelledby="listino-title">
        <div class="catalog-dialog__header">
          <div>
            <h2 id="listino-title">Listino ${escapeHtml(scelto?.name || state.listino.nome || state.listino.fornitore || "fornitore")}</h2>
            <p>${prodotto ? `Cerca la riga che corrisponde a <strong>${escapeHtml(prodotto.name)}</strong>${prodotto.ean ? ` (EAN ${escapeHtml(prodotto.ean)})` : ""}.` : "Il listino come l’ho letto."}</p>
          </div>
          <button type="button" class="dialog-close" data-action="close-listino" aria-label="Chiudi la finestra">×</button>
        </div>
        <div class="listino-dialog__comandi">
          ${/* When the requested supplier can't be browsed it has no entry in
               the list, so without this disabled placeholder option the
               `<select>` would show the first supplier while state points at
               another — selecting it wouldn't fire a `change` event, and the
               dialog would look stuck. */ ""}
          ${state.listino.fornitori.length ? `
          <div class="field">
            <label for="listino-fornitore">Fornitore</label>
            <select id="listino-fornitore" class="select" data-listino-fornitore>
              ${scelto ? "" : `<option value="${escapeHtml(state.listino.fornitore)}" selected disabled>${escapeHtml(state.listino.nome || state.listino.fornitore || "—")} — non si legge</option>`}
              ${state.listino.fornitori.map((voce) => `<option value="${escapeHtml(voce.id)}" ${voce.id === state.listino.fornitore ? "selected" : ""}>${escapeHtml(voce.name)} (${escapeHtml(contati(voce.righe, "riga", "righe"))})</option>`).join("")}
            </select>
          </div>` : state.listino.caricando ? "" : '<p class="listino-dialog__conto">Nessuno dei listini di questo confronto si può sfogliare.</p>'}
          <div class="field field--larga">
            <label for="listino-cerca">Cerca nel listino</label>
            <input id="listino-cerca" class="input" type="search" value="${escapeHtml(state.listino.query)}" placeholder="Nome, EAN o codice del fornitore" data-listino-cerca data-focus-key="listino-cerca">
          </div>
        </div>
        ${state.listino.esito ? `<p class="listino-dialog__esito" role="status">${escapeHtml(state.listino.esito)}</p>` : ""}
        ${state.listino.problema ? renderAlert({ title: `Il listino ${state.listino.nome || state.listino.fornitore || "chiesto"} non si può sfogliare`, message: state.listino.problema, severity: "warning", blocking: false }) : ""}
        ${state.listino.errore ? renderAlert({ title: "Il listino non si legge", message: state.listino.errore, severity: "error", blocking: false }) : ""}
        ${contoDelListino() ? `<p class="listino-dialog__conto">${escapeHtml(contoDelListino())}</p>` : ""}
        <div class="listino-dialog__corpo" aria-live="polite">
          ${state.listino.caricando ? '<div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Lettura del listino…</span></div>' : ""}
          ${!state.listino.caricando && !state.listino.righe.length && !state.listino.problema ? '<div class="empty-state empty-state--small"><div><strong>Nessuna riga</strong><p>Prova con una parola più corta, oppure cambia fornitore.</p></div></div>' : ""}
          ${state.listino.righe.length ? renderTabellaDelListino(prodotto) : ""}
        </div>
        ${renderPagineDelListino()}
      </section>
    </div>`;
}

function contoDelListino() {
  const listino = state.listino;
  // A price list that couldn't be browsed has no rows to count, and "0 rows,
  // all orderable" under the alert explaining why would just contradict it.
  if (listino.problema) return "";
  const cercate = listino.query.trim()
    ? `${contati(listino.trovate, "riga trovata", "righe trovate")} su ${formatInteger(listino.totale)}`
    : contati(listino.totale, "riga", "righe");
  // Discarded rows are always stated: a viewer that shows most rows and
  // silently drops the rest is a viewer that hides things.
  const scartate = listino.scartate
    ? ` · ${formatInteger(listino.scartate)} non ordinabili, mostrate col motivo`
    : " · tutte ordinabili";
  return `${cercate}${scartate}`;
}

function renderTabellaDelListino(prodotto) {
  const abbinate = new Set(
    asArray(prodotto?.offers)
      .filter((offer) => offer.supplierId === state.listino.fornitore && offer.sourceRow != null)
      .map((offer) => String(offer.sourceRow)),
  );
  return `
    <table class="listino-tabella">
      <thead>
        <tr>
          <th scope="col">Riga</th>
          <th scope="col">EAN</th>
          <th scope="col">Descrizione</th>
          <th scope="col">Pezzi per collo</th>
          <th scope="col">Prezzo per pezzo</th>
          <th scope="col"><span class="visually-hidden">Abbina</span></th>
        </tr>
      </thead>
      <tbody>
        ${state.listino.righe.map((riga) => {
          const chiave = String(riga.sourceRow);
          const gia = abbinate.has(chiave);
          const fuoco = String(state.listino.rigaFuoco ?? "") === chiave;
          return `
            <tr class="${gia ? "is-abbinata" : ""}${fuoco ? " is-fuoco" : ""}">
              <td>${escapeHtml(chiave)}</td>
              <td>${riga.ean ? escapeHtml(riga.ean) : "—"}</td>
              <td>
                ${escapeHtml(riga.description || "—")}
                ${riga.supplierCode ? `<span class="listino-tabella__codice">cod. ${escapeHtml(riga.supplierCode)}</span>` : ""}
                ${riga.ordinabile ? "" : `<span class="listino-tabella__scartata">Non ordinabile${riga.motivo ? `: ${escapeHtml(riga.motivo)}` : ""}</span>`}
              </td>
              <td>${riga.piecesPerCarton ? formatInteger(riga.piecesPerCarton) : "—"}</td>
              <td>${riga.unitPriceNet ? formatEuro(riga.unitPriceNet) : "—"}</td>
              <td>
                ${gia
                  ? '<span class="listino-tabella__gia">Già abbinata</span>'
                  : riga.ordinabile
                    ? `<button type="button" class="button button--success button--piccolo" data-action="abbina-riga" data-riga="${escapeHtml(chiave)}" ${state.listino.abbinando ? "disabled" : ""}>${state.listino.abbinando === chiave ? "Attendere…" : "È questo"}</button>`
                    : ""}
              </td>
            </tr>`;
        }).join("")}
      </tbody>
    </table>`;
}

function renderPagineDelListino() {
  const listino = state.listino;
  const totale = listino.query.trim() ? listino.trovate : listino.totale;
  if (totale <= listino.quante) return "";
  const pagina = Math.floor(listino.da / listino.quante) + 1;
  const pagine = Math.max(1, Math.ceil(totale / listino.quante));
  return `
    <div class="listino-dialog__pagine">
      <button type="button" class="button button--secondary" data-action="listino-indietro" ${listino.da <= 0 || listino.caricando ? "disabled" : ""}>← Righe precedenti</button>
      <span>Pagina ${formatInteger(pagina)} di ${formatInteger(pagine)}</span>
      <button type="button" class="button button--secondary" data-action="listino-avanti" ${listino.da + listino.quante >= totale || listino.caricando ? "disabled" : ""}>Righe successive →</button>
    </div>`;
}

function renderCatalogDialog() {
  if (!state.catalog.open) return "";
  const results = state.catalog.results;
  return `
    <div class="dialog-backdrop" role="presentation">
      <section class="catalog-dialog" role="dialog" aria-modal="true" aria-labelledby="catalog-title">
        <div class="catalog-dialog__header">
          <div>
            <h2 id="catalog-title">Aggiungi un prodotto</h2>
            <p>Cerca nei listini caricati un prodotto non presente nell’elenco del gestionale.</p>
          </div>
          <button type="button" class="dialog-close" data-action="close-catalog" aria-label="Chiudi la finestra">×</button>
        </div>
        <div class="field">
          <label for="catalog-search">Nome o codice del prodotto</label>
          <input id="catalog-search" class="input" type="search" value="${escapeHtml(state.catalog.query)}" placeholder="Scrivi almeno due caratteri" data-catalog-search data-focus-key="catalog-search" autofocus>
        </div>
        <div class="catalog-dialog__body" aria-live="polite">
          ${state.catalog.loading ? '<div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Ricerca in corso…</span></div>' : ""}
          ${state.catalog.error ? renderAlert({ title: "Ricerca non riuscita", message: state.catalog.error, severity: "error", blocking: false }) : ""}
          ${!state.catalog.loading && !state.catalog.error && state.catalog.query.trim().length < 2 ? '<div class="empty-state empty-state--small"><div><strong>Inizia a scrivere</strong><p>Puoi cercare per nome o per codice EAN.</p></div></div>' : ""}
          ${!state.catalog.loading && !state.catalog.error && state.catalog.query.trim().length >= 2 && !results.length ? '<div class="empty-state empty-state--small"><div><strong>Nessun prodotto trovato</strong><p>Prova con un nome più breve o con il codice EAN.</p></div></div>' : ""}
          ${results.length ? `<ul class="catalog-results">${results.map((product) => `
            <li>
              <div>
                <strong>${escapeHtml(product.name)}</strong>
                <span>${product.ean ? `EAN ${escapeHtml(product.ean)} · ` : ""}${product.suppliersCount ? `${formatInteger(product.suppliersCount)} fornitori` : escapeHtml(product.supplierName || "Disponibile nei listini")}${product.bestPrice ? ` · da ${formatEuro(product.bestPrice)}` : ""}</span>
              </div>
              <button type="button" class="button ${product.alreadyPresent ? "button--secondary" : "button--primary"}" data-action="add-catalog-product" data-catalog-product-id="${escapeHtml(product.id)}" ${product.alreadyPresent ? "disabled" : ""}>${product.alreadyPresent ? "Già presente" : "Aggiungi"}</button>
            </li>`).join("")}</ul>` : ""}
        </div>
        <div class="catalog-dialog__footer">
          <button type="button" class="button button--secondary" data-action="close-catalog">Chiudi</button>
        </div>
      </section>
    </div>`;
}

// The supplier comparison is a table, not one box per supplier repeating the
// same four labels ("Price per carton", "Price per piece", "Pieces per
// carton", "Total for this quantity"): across twenty products per page that
// would be a lot of labels read to reach a lot of numbers, with the numbers
// not aligned in columns — the exact thing a comparison exists to allow.
//
// The labels stay in the markup: the stylesheet hides them once the header
// row covers their job, and shows them again on narrow screens, where the
// table stacks. A screen reader announces them either way.
//
// Also matters — especially — when the barcode matches but the product still
// differs: a colored item can share an EAN across variants while looking
// different on the shelf. The supplier's own product name is the only place
// that variant detail is written, even though the service has always sent
// it — so the row includes it, rather than showing only the numbers.
//
// The EAN is only shown when it differs from the product's own: matching
// adds nothing and is already in the card's header; a mismatch means this row
// was matched through some other signal, which is the first thing worth
// checking.
function rigaDelFornitore(product, offer) {
  const pezzi = [];
  if (offer.description) pezzi.push(escapeHtml(offer.description));
  if (offer.ean && offer.ean !== product.ean) pezzi.push(`EAN ${escapeHtml(offer.ean)}`);
  if (offer.supplierCode) pezzi.push(`cod. ${escapeHtml(offer.supplierCode)}`);
  if (!pezzi.length) return "";
  return `<span class="offer-card__riga-fornitore" title="Come lo chiama ${escapeHtml(offer.supplierName)} sul suo listino">${pezzi.join(" · ")}</span>`;
}

function renderOfferGrid(product) {
  if (!product.offers.length) {
    return '<div class="empty-state"><div><strong>Nessun fornitore ce l’ha</strong><p>Il prodotto resta nell’elenco, ma non può essere assegnato a nessun fornitore.</p></div></div>';
  }

  const unitLabel = orderUnitSingular(product.orderUnitLabel);
  const intestazioni = [
    "Fornitore",
    `Prezzo per ${unitLabel}`,
    "Prezzo per pezzo",
    `Pezzi per ${unitLabel}`,
    "Totale per la quantità",
    "Rispetto all’ultimo pagato",
  ];
  const cella = (etichetta, contenuto, extra = "") => `
    <span class="offer-card__cell${extra}">
      <span class="offer-card__label">${escapeHtml(etichetta)}</span>
      ${contenuto}
    </span>`;

  const ordinate = product.offers.slice()
    .sort((a, b) => Number(b.available) - Number(a.available) || a.pricePerPiece - b.pricePerPiece);
  const disponibili = ordinate.filter((offer) => offer.available);
  // How much each supplier costs above the cheapest one. The minimum is
  // taken only among AVAILABLE offers: on a product where the cheapest row
  // isn't usable, differences would otherwise be shown against a price
  // nobody can actually order. Always per piece, never on the carton total —
  // that rule is stated twice in this file because it's easy to break.
  const minimoAlPezzo = disponibili.length
    ? Math.min(...disponibili.map((offer) => finiteNumber(offer.pricePerPiece)))
    : 0;
  // A supplier without the product deserves one shared line, not a repeated
  // box saying the same thing for each one; a supplier with its own reason
  // keeps it. There are three reasons an offer isn't usable: no matching
  // row, a row the user rejected ("you said no" isn't "they don't have it"),
  // or a row already shown elsewhere on the card as a pending candidate to
  // confirm. That third case is excluded from this generic list too — it
  // would otherwise contradict the candidate box shown right above it on the
  // same card.
  const conCandidatoInPagina = new Set([
    ...issuesForProduct(product)
      .filter((issue) => issue.code === "RIFIUTO_CON_CANDIDATO_FORTE")
      .map((issue) => issue.supplierId),
    ...product.offers
      .filter((offer) => offer.rejectedCandidate
        && (String(offer.candidateDecision) === "accepted" || String(offer.candidateDecision) === "rejected"))
      .map((offer) => offer.supplierId),
  ].filter(Boolean));
  const rifiutate = ordinate.filter((offer) => !offer.available && offer.rifiutata);
  const senzaMotivo = ordinate.filter((offer) => !offer.available && !offer.rifiutata && !offer.warning
    && !conCandidatoInPagina.has(offer.supplierId));
  const conMotivo = ordinate.filter((offer) => !offer.available && !offer.rifiutata && offer.warning);

  return `
    <div class="offer-grid">
      ${disponibili.length ? `<div class="offer-grid__head" aria-hidden="true">${intestazioni.map((voce) => `<span>${escapeHtml(voce)}</span>`).join("")}</div>` : ""}
      ${disponibili.map((offer) => {
        const selected = offer.supplierId === product.selectedSupplierId;
        const currentCalculation = offerCalculation(product, offer, orderQuantity(product));
        const lastPrice = lastPriceComparison(product, offer);
        return `
          <label class="offer-card${selected ? " is-selected" : ""}">
            <span class="offer-card__cell offer-card__identita">
              <span class="offer-card__supplier">
                <input type="radio" name="offer-${escapeHtml(product.id)}" value="${escapeHtml(offer.supplierId)}" data-offer-choice="${escapeHtml(product.id)}" data-focus-key="offerta-${escapeHtml(product.id)}-${escapeHtml(offer.supplierId)}" ${selected ? "checked" : ""}>
                ${escapeHtml(offer.supplierName)}
              </span>
              ${abbinamentoNormale(offer)
                ? `<span class="offer-card__abbinamento">${escapeHtml(offer.matchStatus)}</span>`
                : renderBadge(offer.matchStatus, offer.compositionStatus === "comparable" ? "warning" : "success")}
              ${rigaDelFornitore(product, offer)}
            </span>
            ${cella(intestazioni[1], `<strong>${formatEuro(offer.orderUnitPriceNet)}</strong>`, " offer-card__numero")}
            ${cella(
              intestazioni[2],
              // Half-cent tolerance: below it, the delta would print as
              // "+0.00 €/pz", which is just noise.
              `<strong>${formatEuro(offer.pricePerPiece)}</strong>${finiteNumber(offer.pricePerPiece) - minimoAlPezzo > 0.005
                ? `<span class="offer-card__delta">+${formatEuro(finiteNumber(offer.pricePerPiece) - minimoAlPezzo)}/pz</span>`
                : ""}`,
              " offer-card__numero",
            )}
            ${cella(intestazioni[3], `<strong>${formatInteger(offer.quantityFactor)}</strong>`, " offer-card__numero")}
            ${cella(intestazioni[4], `<strong>${orderQuantity(product) > 0 ? formatEuro(currentCalculation.total) : "—"}</strong>`, " offer-card__numero offer-card__cell--totale")}
            ${cella(
              intestazioni[5],
              lastPrice
                ? `<span class="offer-card__last-price ${lastPrice.amount <= 0 ? "is-lower" : "is-higher"}">${lastPrice.amount <= 0 ? "▼" : "▲"} ${formatEuro(Math.abs(lastPrice.amount))}/pz</span>`
                : '<span class="offer-card__niente">—</span>',
              " offer-card__numero",
            )}
            ${offer.promotion ? `<span class="offer-card__intera promotion"><strong>Offerta:</strong> ${escapeHtml(offer.promotion)}</span>` : ""}
            ${offer.warning ? `<span class="offer-card__intera offer-card__delta">${escapeHtml(offer.warning)}</span>` : ""}
          </label>`;
      }).join("")}
      ${conMotivo.map((offer) => `
        <div class="offer-card is-unavailable">
          <span class="offer-card__cell offer-card__identita">
            <span class="offer-card__supplier">${escapeHtml(offer.supplierName)}</span>
            ${renderBadge("Non disponibile", "neutral")}
          </span>
          <span class="offer-card__intera offer-card__unavailable">${escapeHtml(offer.warning)}</span>
        </div>`).join("")}
      ${/* The rule explaining how long a rejection lasts is shown once below,
           not inside every box — it's the same sentence for every supplier.
           Each box keeps only what differs: the rejected row, the date, and
           the way to reverse it. */ ""}
      ${rifiutate.map((offer) => {
        const busy = Boolean(state.matches.answering);
        const answering = state.matches.answering === `${product.id}:${offer.supplierId}:rifiuto`;
        const giorno = formatDayMonth(offer.rifiutata.since);
        return `
        <div class="offer-card is-unavailable is-rifiutata">
          <span class="offer-card__cell offer-card__identita">
            <span class="offer-card__supplier">${escapeHtml(offer.supplierName)}</span>
            ${renderBadge("Rifiutata da te", "neutral")}
          </span>
          <span class="offer-card__intera offer-card__unavailable">
            Hai detto che «${escapeHtml(offer.rifiutata.description || offer.description || "la riga proposta")}» non è lo
            stesso articolo${giorno ? `, il ${escapeHtml(giorno)}` : ""}.
            <button type="button" class="button button--ghost button--piccolo" data-action="rifiuta-abbinamento"
              data-focus-key="rifiuto-${escapeHtml(product.id)}-${escapeHtml(offer.supplierId)}"
              data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(offer.supplierId)}"
              data-rifiutata="false" ${busy ? "disabled" : ""}>${answering ? "Attendere…" : "Cambio idea: è lo stesso"}</button>
          </span>
        </div>`;
      }).join("")}
      ${rifiutate.length ? `<p class="offer-grid__regola-del-no">Un no vale finché quel fornitore propone quella riga:
        se la settimana prossima ne propone un'altra, la domanda torna.</p>` : ""}
      ${senzaMotivo.length ? `<p class="offer-grid__mancanti">${escapeHtml(elencoNomi(senzaMotivo.map((offer) => offer.supplierName)))}: non ce l’hanno nel listino di adesso.</p>` : ""}
      ${renderApriIlListino(product)}
    </div>`;
}

// The fallback for when automatic matching couldn't find a row, and
// structurally couldn't: the management name doesn't carry the variant, the
// supplier uses a different barcode, and no scoring heuristic can guess it.
// Opens on the supplier that already has a matched row for this product —
// to double-check it's really the same item — or on the first price list, to
// search from scratch.
function renderApriIlListino(product) {
  if (mode === "demo") return "";
  // The selected supplier goes first; only when it has no matched row does
  // this fall back to the first one that does. `.find()` alone would always
  // return the first supplier in `build_review_data.supplier_ids()`'s fixed
  // order, which only coincidentally matches the selected supplier.
  const scelta = selectedOffer(product);
  const conRiga = (scelta && scelta.sourceRow != null ? scelta : null)
    || product.offers.find((offer) => offer.available && offer.sourceRow != null);
  // When nobody has a matched row — the "search by hand" case — the same
  // preference applies: opens on the selected supplier, not the first one.
  const primo = conRiga || scelta || product.offers[0];
  // The label is deliberately generic rather than naming one supplier: the
  // dialog it opens carries the name as its title plus a dropdown to switch
  // between every supplier without closing it, so a single supplier's name
  // on the button would promise less than the dialog actually does.
  //
  // Which price list opens FIRST is still the choice made above — the
  // selected supplier, not the first in the list.
  const etichetta = "Sfoglia i listini";
  return `
    <p class="offer-grid__sfoglia">
      <button type="button" class="button button--ghost button--piccolo" data-action="apri-listino"
        data-focus-key="apri-listino-${escapeHtml(product.id)}"
        data-product-id="${escapeHtml(product.id)}"
        data-supplier-id="${escapeHtml(primo?.supplierId || "")}"
        data-riga="${escapeHtml(conRiga?.sourceRow ?? "")}">${escapeHtml(etichetta)}</button>
      <span>Per controllare la riga di un fornitore, o trovarla a mano quando ha un codice diverso.</span>
    </p>`;
}

// The local service combines two sources when deciding whether confirmation
// is required: the product (uncertain match) and the selected offer (inexact
// match, low-confidence display). This must use the same rule, or a save can
// be rejected by the service with no matching checkbox anywhere on the page.
function confirmationRequired(product) {
  // Without a selected supplier there's nothing to confirm — this mirrors
  // the service's own gate (`validate_snapshot`: `requires =
  // bool(supplier_id) and offer_needs_confirmation(...)`). Without this
  // check, `product.requiresConfirmation` alone could keep a blocking
  // "confirmation required" state alive for a product with no offer left to
  // confirm.
  const offer = selectedOffer(product);
  if (!offer) return false;
  if (product.requiresConfirmation) return true;
  return Boolean(offer.requiresConfirmation);
}

function confirmationMessageFor(product) {
  if (product.requiresConfirmation && product.confirmationMessage) return product.confirmationMessage;
  const offer = selectedOffer(product);
  if (offer?.confirmationMessage) return offer.confirmationMessage;
  if (offer?.requiresConfirmation) {
    return `La riga di ${offer.supplierName} non è un abbinamento esatto: controlla che sia davvero lo stesso articolo prima di ordinarlo.`;
  }
  return product.confirmationMessage;
}

// The confirmation already given, when there is one: the confirmation store
// (`app/conferme.py`) keeps it, and the service attaches it to the product as
// `confirmation: {supplierId, since, article}`.
//
// Two things decide whether the checkmark can be trusted: when it was
// given, and that it covers the item — barcode plus name — not the
// price-list row. That's why it survives into next week's price list even
// though that row has changed: without saying so, an already-checked box
// would look like a stale leftover worth re-verifying.
//
// Revocation is also surfaced here: the service already supports clearing a
// confirmation, but nothing on the page explained the gesture, and it's the
// only way out of a confirmation given by mistake.
function renderConfermaGiaData(product) {
  const conferma = product.confirmation;
  // The flag, not just the stored record: right after the user revokes a
  // confirmation, the store still has it — it clears on save — so "already
  // confirmed" would contradict a question that's visibly reopened. Once
  // revoked, the reopened question needs to show why confirmation was asked
  // for in the first place.
  if (!conferma || !product.confirmed) return "";
  const giorno = formatDayMonth(conferma.since);
  return `
    <p class="confirmation__gia">
      <strong>Già confermato da te${giorno ? ` il ${escapeHtml(giorno)}` : ""}.</strong>
      Vale per l’articolo — codice a barre e nome —, non per la riga del listino:
      resta valida anche con il listino della settimana prossima.
    </p>`;
}

// What actually happens to this product if the answer is no — computed here
// rather than left as a generic sentence, since the outcome (which supplier
// takes over, or that none does) is specific to this product and this offer.
//
// The rule for who takes over matches `normalizeReview`: the cheapest
// available offer BY PIECE. It isn't recomputed a second way here — two
// authorities on the same value would drift apart the day one of them
// changes.
function conseguenzaDelNo(product, offer) {
  const prossimo = asArray(product?.offers)
    .filter((candidato) => candidato.available && candidato.supplierId !== offer.supplierId)
    .sort((a, b) => a.pricePerPiece - b.pricePerPiece)[0];
  if (!prossimo) {
    // "The only one who has it" would be false in exactly the case this
    // sentence is most likely to be read: the others do have the row, the
    // user said it's a different item. What matters is that this is the
    // last one left.
    return `${offer.supplierName} è l’ultimo fornitore rimasto su questo prodotto: senza di lui resta `
      + "senza fornitore, con la sua quantità, e alla compilazione finisce nell’elenco «Prodotti da reperire».";
  }
  return `${offer.supplierName} esce da questo prodotto e l’ordine passa a ${prossimo.supplierName}, `
    + `${formatEuro(prossimo.pricePerPiece)} al pezzo.`;
}

// The confirmation question, with its two answers, matching this card's twin
// (`renderCandidateDecisions`, "Is it the same product? Yes / No"): two
// buttons, the same pending state, the same wording.
//
// A question that's already been answered isn't asked again: the page states
// the answer and offers a way to change it, exactly like a rejection behaves
// in the offer grid.
function renderConfirmation(product) {
  if (!confirmationRequired(product) || orderQuantity(product) <= 0) return "";
  const offer = selectedOffer(product);
  const gia = renderConfermaGiaData(product);
  const attesa = String(state.matches.answering || "");
  const occupato = Boolean(attesa);
  const attendeIlSi = attesa === `${product.id}:conferma`;
  const attendeIlNo = offer ? attesa === `${product.id}:${offer.supplierId}:rifiuto` : false;
  return `
    <div class="confirmation${gia ? " is-answered" : ""}">
      <div>
        <strong>${escapeHtml(product.confirmed ? "È lo stesso articolo" : "È lo stesso articolo?")}</strong>
        <span>Richiesto: <strong>${escapeHtml(product.name)}</strong></span>
        ${offer?.description ? `<span>${escapeHtml(offer.supplierName)} propone: <strong>${escapeHtml(offer.description)}</strong></span>` : ""}
        ${/* The reason is what's needed to decide, so it's shown directly
             rather than behind a "why?" disclosure. Not needed once the
             question is already answered. */ ""}
        ${gia || `<p class="confirmation__why">${escapeHtml(confirmationMessageFor(product))}</p>`}
        ${mode === "demo" ? "" : product.confirmed ? `
        <p class="confirmation__risposte">
          <button type="button" class="button button--ghost button--piccolo" data-action="conferma-abbinamento"
            data-focus-key="conferma-${escapeHtml(product.id)}"
            data-product-id="${escapeHtml(product.id)}" data-confermato="false"
            ${occupato ? "disabled" : ""}>${attendeIlSi ? "Attendere…" : "Cambio idea"}</button>
          <span>Riapre la domanda: la conferma viene tolta e il prodotto torna fra quelli da confermare.</span>
        </p>` : `
        <p class="confirmation__risposte">
          ${/* "Yes" comes first and is the emphasized action: it closes the
               question without removing anything. */ ""}
          <button type="button" class="button button--primary button--piccolo" data-action="conferma-abbinamento"
            data-focus-key="conferma-${escapeHtml(product.id)}"
            data-product-id="${escapeHtml(product.id)}" data-confermato="true"
            ${occupato ? "disabled" : ""}>${attendeIlSi ? "Attendere…" : "Sì, è lo stesso"}</button>
          ${/* The "No" answer. Without it, the only ways out of a blocking
               confirmation were confirming anyway or excluding the product
               entirely — the first is a wrong order, the second drops it from
               the "to be sourced" list too, since that list skips
               zero-quantity items. Not shown in demo mode: the service
               doesn't respond there, and a button that errors on every press
               isn't a real question. */ ""}
          ${!offer ? "" : `
          <button type="button" class="button button--secondary button--piccolo" data-action="rifiuta-abbinamento"
            data-focus-key="rifiuto-${escapeHtml(product.id)}-${escapeHtml(offer.supplierId)}"
            data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(offer.supplierId)}"
            aria-describedby="rifiuto-nota-${escapeHtml(product.id)}"
            data-rifiutata="true" ${occupato ? "disabled" : ""}>${
              /* Shows a waiting state rather than just going gray between the
                 click and the re-rendered page (a save, the response, the
                 reread comparison). Checks THIS specific answer: while
                 another card's answer is pending, this button is disabled
                 but not itself waiting. */ ""
            }${attendeIlNo ? "Attendere…" : "No, non è lo stesso"}</button>
          <span id="rifiuto-nota-${escapeHtml(product.id)}">${escapeHtml(conseguenzaDelNo(product, offer))}</span>`}
        </p>`}
      </div>
    </div>`;
}

// The supplier's blanket discount, as a percentage. Placed where its money
// shows up — the totals strip — because that's where the effect is visible:
// typing 6 drops that supplier's prices everywhere, including which supplier
// wins each product, which the service recomputes on its own.
function renderStickySupplierTotals() {
  const sconti = state.review?.supplierDiscounts || {};
  return `
    <aside class="supplier-totals-strip" aria-label="Totali ordine per fornitore">
      <span class="supplier-totals-strip__label">Totali ordine</span>
      ${supplierTotals().map((entry) => {
        // Already a plain number here: `normalizeSupplierDiscounts` converts
        // it to a percentage, at the boundary where service data becomes page
        // data. Recomputing it here would create a second authority on the
        // same value.
        const sconto = sconti[entry.supplier.id] || 0;
        return `<span class="supplier-totals-strip__item${entry.total > 0 ? " is-active" : ""}${sconto ? " has-discount" : ""}">
          <span>${escapeHtml(entry.supplier.name)}</span>
          <strong>${formatEuro(entry.total)}</strong>
          ${/* The label is visible text, not just a `title` tooltip: a
               tooltip only appears after the mouse hovers, so keyboard and
               touch users never saw it before. The compact size is a
               deliberate stylesheet decision, not an oversight. */ ""}
          <label class="supplier-discount" title="Sconto su tutto il listino di ${escapeHtml(entry.supplier.name)}">
            <span class="supplier-discount__etichetta">sconto</span>
            −<input class="supplier-discount__input" type="number" min="0" max="99" step="0.5" inputmode="decimal"
              value="${sconto ? escapeHtml(sconto) : ""}" placeholder="0"
              data-supplier-discount="${escapeHtml(entry.supplier.id)}"
              data-focus-key="discount-${escapeHtml(entry.supplier.id)}"
              aria-label="Sconto percentuale su tutte le offerte di ${escapeHtml(entry.supplier.name)}">%
          </label>
        </span>`;
      }).join("")}
      ${/* Kept visible here, in the sticky strip, rather than in the list
           header that scrolls out of view: this is exactly when it's looked
           at, while quantities are being changed. It's the sum of the items
           beside it — the same number, not a second one. */ ""}
      <span class="supplier-totals-strip__item supplier-totals-strip__item--totale">
        <span>Totale</span>
        <strong>${formatEuro(allOrderTotal())}</strong>
      </span>
    </aside>`;
}

// No rounding-difference line under supplier and grand totals: a rounding
// difference of a cent or two never changes an ordering decision, so it
// would only take up a line next to the number that does matter. The local
// service still sends `roundingDifference` in `orderSummary` (server.py) and
// is still tested for it, for anyone reconciling totals by hand.

// Each supplier's product list collapses by clicking its name, with the
// subtotal staying visible. With hundreds of rows, reaching the next
// supplier otherwise meant scrolling past everything in between.
//
// Starts OPEN — that's how it's normally read, and a summary that defaults
// to closed would hide what the user came to look at — so the persisted
// state has to track "closed", not "open": the key lives in `state.aperti`
// like every other collapsible, via `apribile(chiave, true)`. Without that
// inversion, the summary would reopen on its own on the next quantity
// change, which re-renders everything.
//
// The key includes the supplier id, so two suppliers don't open and close
// together.
function renderSupplierSummary(entry) {
  if (!entry.products.length) return "";
  const reached = entry.minimumOrder <= 0 || entry.total >= entry.minimumOrder;
  const products = entry.products.slice().sort((a, b) => {
    if (state.summary.sort === "total") return b.subtotal - a.subtotal;
    return a.product.name.localeCompare(b.product.name, "it", { sensitivity: "base" });
  });
  return `
    <details class="supplier-summary" ${apribile(`riepilogo-fornitore:${entry.supplier.id}`, true)}>
      ${/* The row count sits next to the name and is what states what's
           hidden when the panel is collapsed: closed, it still reads as
           "supplier · N rows · N order units" plus its total. */ ""}
      <summary class="supplier-summary__head">
        <div class="supplier-summary__chi">
          <strong>${escapeHtml(entry.supplier.name)}</strong>
          <span>${entry.lines} ${entry.lines === 1 ? "riga" : "righe"} · ${entry.units} unità d'ordine</span>
        </div>
        <div class="supplier-summary__total">
          <strong>${formatEuro(entry.total)}</strong>
          <span>${entry.minimumOrder > 0 ? (reached ? `minimo d’ordine ${formatEuro(entry.minimumOrder)} raggiunto` : `mancano ${formatEuro(entry.minimumOrder - entry.total)} al minimo d’ordine`) : "nessun minimo d’ordine"}</span>
        </div>
      </summary>
      ${renderSupplierMoveCommand(entry)}
      <ul class="order-lines">
        <li class="order-lines__intestazione" aria-hidden="true"><span>Prodotto</span><span>Quantità</span><span></span><span>Prezzo al pezzo</span><span>Totale</span></li>
        ${products.map(({ product, offer, quantity, calculation, subtotal }) => `
          <li>
            <span class="order-lines__name">${escapeHtml(product.name)}<small class="order-lines__ean">EAN fornitore: ${escapeHtml(offer.ean || "non presente")}</small>${offer.promotion ? `<small>Offerta: ${escapeHtml(offer.promotion)}</small>` : ""}</span>
            <span class="order-lines__qty">${escapeHtml(`${formatInteger(quantity)} ${quantity === 1 ? orderUnitSingular(product.orderUnitLabel) : product.orderUnitLabel}`)}</span>
            ${renderSummaryQuantityControls(product)}
            ${renderPrezzoAlPezzo(calculation)}
            <strong>${formatEuro(subtotal)}</strong>
          </li>`).join("")}
      </ul>
    </details>`;
}

// --- Move everything to another supplier (page 3) --------------------------
// The use case: a supplier's total is below the minimum order, so its
// products get reassigned to the next best supplier for each one. The cost
// difference is shown BEFORE the decision, but the browser doesn't compute
// it: every figure here comes from `POST /api/suppliers/move-preview`. The
// carton count stays the same; only the supplier changes, and with it the
// price and the actual piece count.

// Reasons arrive from the local service as codes and are translated here. An
// unrecognized code must never hide the product's name, so there's always a
// fallback sentence. These are the only two reason codes the local service
// actually emits — if a code the service never sends were to appear as a key
// here, the product would land in the fallback and the user wouldn't know
// why it stayed put.
const MOVE_LEFT_BEHIND_REASONS = {
  NESSUNA_OFFERTA: {
    supplier: "questo fornitore non ha il prodotto",
    best: "nessun altro fornitore ha il prodotto",
  },
  PREZZO_NON_DISPONIBILE: {
    supplier: "il listino di questo fornitore non riporta un prezzo utilizzabile",
    best: "nessun altro listino riporta un prezzo utilizzabile",
  },
};

function moveLeftBehindReason(reason, kind) {
  const entry = MOVE_LEFT_BEHIND_REASONS[String(reason || "").trim().toUpperCase()];
  if (!entry) return "non può essere spostato";
  return kind === "best" ? entry.best : entry.supplier;
}

function normalizeMoveAssignment(assignment) {
  return {
    productId: String(assignment?.productId ?? ""),
    productName: String(assignment?.productName ?? ""),
    toSupplierId: String(assignment?.toSupplierId ?? ""),
    previousFactor: Math.max(0, Math.trunc(finiteNumber(assignment?.previousFactor, 0))),
    newFactor: Math.max(0, Math.trunc(finiteNumber(assignment?.newFactor, 0))),
    factorChanged: Boolean(assignment?.factorChanged),
    previousPieces: Math.max(0, Math.trunc(finiteNumber(assignment?.previousPieces, 0))),
    newPieces: Math.max(0, Math.trunc(finiteNumber(assignment?.newPieces, 0))),
    needsConfirmation: Boolean(assignment?.needsConfirmation),
    previousLineNet: finiteNumber(assignment?.previousLineNet, 0),
    newLineNet: finiteNumber(assignment?.newLineNet, 0),
  };
}

function normalizeMoveSupplierTotal(row) {
  const supplierId = String(row?.supplierId ?? "");
  return {
    supplierId,
    supplierName: String(row?.supplierName ?? "") || supplierName(supplierId),
    netTotalBefore: finiteNumber(row?.netTotalBefore, 0),
    netTotalAfter: finiteNumber(row?.netTotalAfter, 0),
    giftsBefore: Math.max(0, Math.trunc(finiteNumber(row?.giftsBefore, 0))),
    giftsAfter: Math.max(0, Math.trunc(finiteNumber(row?.giftsAfter, 0))),
    threshold: Math.max(0, finiteNumber(row?.threshold, 0)),
    hadOrderBefore: Boolean(row?.hadOrderBefore),
    meetsThresholdBefore: Boolean(row?.meetsThresholdBefore),
    meetsThresholdAfter: Boolean(row?.meetsThresholdAfter),
  };
}

function normalizeMoveOption(option, index) {
  const kind = String(option?.kind ?? "") === "best" ? "best" : "supplier";
  const id = String(option?.id ?? "") || (kind === "best" ? "best" : `opzione-${index + 1}`);
  return {
    id,
    kind,
    label: String(option?.label ?? "") || (kind === "best" ? "Migliore alternativa per ciascun prodotto" : supplierName(id)),
    movedCount: Math.max(0, Math.trunc(finiteNumber(option?.movedCount, 0))),
    movableCount: Math.max(0, Math.trunc(finiteNumber(option?.movableCount, 0))),
    deltaNet: finiteNumber(option?.deltaNet, 0),
    deliveredPiecesBefore: Math.max(0, Math.trunc(finiteNumber(option?.deliveredPiecesBefore, 0))),
    deliveredPiecesAfter: Math.max(0, Math.trunc(finiteNumber(option?.deliveredPiecesAfter, 0))),
    deltaPieces: Math.trunc(finiteNumber(option?.deltaPieces, 0)),
    costPerPieceBefore: finiteNumber(option?.costPerPieceBefore, 0),
    costPerPieceAfter: finiteNumber(option?.costPerPieceAfter, 0),
    deltaCostPerPiece: finiteNumber(option?.deltaCostPerPiece, 0),
    // Threshold gifts (free goods): how many are lost and gained. The local
    // service counts them from the promotion rules; their monetary value is
    // deliberately not computed.
    giftsBefore: Math.max(0, Math.trunc(finiteNumber(option?.giftsBefore, 0))),
    giftsAfter: Math.max(0, Math.trunc(finiteNumber(option?.giftsAfter, 0))),
    giftsLost: Math.max(0, Math.trunc(finiteNumber(option?.giftsLost, 0))),
    giftsGained: Math.max(0, Math.trunc(finiteNumber(option?.giftsGained, 0))),
    assignments: asArray(option?.assignments)
      .map(normalizeMoveAssignment)
      .filter((assignment) => assignment.productId && assignment.toSupplierId),
    leftBehind: asArray(option?.leftBehind).map((item) => ({
      productId: String(item?.productId ?? ""),
      productName: String(item?.productName ?? ""),
      reason: String(item?.reason ?? ""),
    })),
    supplierTotalsAfter: asArray(option?.supplierTotalsAfter).map(normalizeMoveSupplierTotal),
  };
}

function normalizeMovePreview(payload) {
  return {
    from: String(payload?.from ?? ""),
    fromName: String(payload?.fromName ?? ""),
    movableCount: Math.max(0, Math.trunc(finiteNumber(payload?.movableCount, 0))),
    // Read but not displayed: the contract doesn't say whether this is the
    // whole order's total or just the source supplier's, and a figure with
    // the wrong label would be worse than no figure. The source supplier's
    // total is already in the dialog title, taken from the card the user sees.
    currentNetTotal: finiteNumber(payload?.currentNetTotal, 0),
    // A destination that moves nothing isn't a real choice, so it's dropped.
    options: asArray(payload?.options)
      .map(normalizeMoveOption)
      .filter((option) => option.movedCount > 0 && option.assignments.length > 0),
  };
}

// The cost difference is shown even when it's favorable. Color alone isn't
// enough: the figure is always paired with a sentence saying what it means.
// The cost difference alone is misleading, the same trap as comparing offers:
// cartons from different suppliers don't hold the same piece count, so an
// option that costs less can deliver half the goods. Both are always stated
// together, and "you save" only appears when the goods quantity doesn't drop.
function moveDeltaParts(option) {
  const amount = finiteNumber(option?.deltaNet, 0);
  const pieces = Math.trunc(finiteNumber(option?.deltaPieces, 0));
  const spendsMore = amount > 0.005;
  const spendsLess = amount < -0.005;
  const text = spendsMore ? `+${formatEuro(amount)}` : formatEuro(spendsLess ? amount : 0);
  if (spendsLess && pieces < 0) return { text, tone: "is-higher", words: "si spende meno, ma arriva meno merce" };
  if (spendsLess) return { text, tone: "is-lower", words: "si risparmia" };
  if (spendsMore && pieces > 0) return { text, tone: "is-neutral", words: "si spende di più, ma arriva più merce" };
  if (spendsMore) return { text, tone: "is-higher", words: "si spende di più" };
  if (pieces < 0) return { text, tone: "is-higher", words: "stessa spesa, meno merce" };
  if (pieces > 0) return { text, tone: "is-lower", words: "stessa spesa, più merce" };
  return { text, tone: "is-equal", words: "stessa spesa" };
}

// The honest number for comparing two options is the cost per piece: the only
// one that doesn't depend on how much a carton holds.
function movePieceTexts(option) {
  const pieces = Math.trunc(finiteNumber(option?.deltaPieces, 0));
  const goods = pieces === 0
    ? `stessa merce: ${formatInteger(option.deliveredPiecesAfter)} pezzi`
    : `merce: da ${formatInteger(option.deliveredPiecesBefore)} a ${formatInteger(option.deliveredPiecesAfter)} pezzi (${pieces > 0 ? "+" : "−"}${formatInteger(Math.abs(pieces))})`;
  const unit = `al pezzo: da ${formatEuro(option.costPerPieceBefore)} a ${formatEuro(option.costPerPieceAfter)}`;
  return { goods, unit };
}

// Thresholds are the reason a move happens, so who reaches theirs and who
// doesn't is always stated, using the total from the local service. Note
// `hadOrderBefore`: the service reports "threshold met" even for a supplier
// with no order at all, since a supplier that doesn't order isn't below
// threshold. Without that check, a supplier that started at zero would be
// reported as having "dropped below" a threshold it never had.
function moveThresholdNotes(option) {
  return asArray(option?.supplierTotalsAfter)
    .filter((row) => row.threshold > 0)
    .map((row) => {
      const reachedBefore = row.hadOrderBefore && row.meetsThresholdBefore;
      if (row.netTotalAfter <= 0) {
        if (!row.hadOrderBefore) return null;
        return { tone: "is-good", text: `${row.supplierName} resta senza prodotti: non c’è più un ordine da portare a ${formatEuro(row.threshold)}.` };
      }
      if (row.meetsThresholdAfter && !reachedBefore) {
        return { tone: "is-good", text: `${row.supplierName} passa a ${formatEuro(row.netTotalAfter)}, minimo d’ordine raggiunto.` };
      }
      if (!row.meetsThresholdAfter && reachedBefore) {
        return { tone: "is-bad", text: `${row.supplierName} scende a ${formatEuro(row.netTotalAfter)} e non raggiunge più il minimo d’ordine di ${formatEuro(row.threshold)}.` };
      }
      if (!row.meetsThresholdAfter) {
        return { tone: "is-warn", text: `${row.supplierName} arriva a ${formatEuro(row.netTotalAfter)}: minimo d’ordine di ${formatEuro(row.threshold)} non raggiunto.` };
      }
      return null;
    })
    .filter(Boolean);
}

// Gifts lost by moving the goods elsewhere. A gift threshold is met against a
// single supplier's order, so moving products away from it breaks the
// threshold. Only the gift count is stated, never its value: what a gift is
// worth is a commercial decision outside this program's scope.
function moveGiftNotes(option) {
  const notes = [];
  const persi = Math.max(0, Math.trunc(finiteNumber(option?.giftsLost, 0)));
  const guadagnati = Math.max(0, Math.trunc(finiteNumber(option?.giftsGained, 0)));
  // Per-supplier detail: "2 before, 2 after" without saying who loses and who
  // gains hides a swap between suppliers of goods that aren't the same. The
  // service counts losses and gains per supplier, and the page shows them
  // per supplier.
  const movimenti = asArray(option?.supplierTotalsAfter)
    .filter((row) => Math.trunc(finiteNumber(row?.giftsBefore, 0)) !== Math.trunc(finiteNumber(row?.giftsAfter, 0)))
    .map((row) => `${row.supplierName || row.supplierId} ${formatInteger(finiteNumber(row.giftsBefore, 0))}→${formatInteger(finiteNumber(row.giftsAfter, 0))}`)
    .join("; ");
  const dettaglio = movimenti ? ` (${movimenti})` : "";
  if (persi) {
    notes.push({
      tone: "is-bad",
      text: `Si ${persi === 1 ? "perde 1 omaggio" : `perdono ${formatInteger(persi)} omaggi`} di soglia${dettaglio}.`,
    });
  }
  if (guadagnati) {
    notes.push({
      tone: "is-good",
      text: `Si ${guadagnati === 1 ? "guadagna 1 omaggio" : `guadagnano ${formatInteger(guadagnati)} omaggi`} di soglia${dettaglio}.`,
    });
  }
  return notes;
}

function moveLeftBehindText(option, fromName) {
  if (!option.leftBehind.length) return "";
  // Products left behind are named individually: a bare count wouldn't say
  // what's staying where it was.
  const names = option.leftBehind
    .map((item) => `${item.productName || "prodotto senza nome"} (${moveLeftBehindReason(item.reason, option.kind)})`)
    .join("; ");
  const count = option.leftBehind.length;
  return `${formatInteger(count)} ${count === 1 ? "prodotto resta" : "prodotti restano"} a ${fromName}: ${names}.`;
}

function supplierMoveFromName() {
  return state.supplierMove.preview?.fromName || state.supplierMove.fromName || supplierName(state.supplierMove.from);
}

function selectedSupplierMoveOption() {
  const preview = state.supplierMove.preview;
  if (!preview) return null;
  return preview.options.find((option) => option.id === state.supplierMove.choiceId) || null;
}

// The command sits on every supplier card and stays secondary even below
// threshold: making it primary there would mean one primary button per
// under-threshold supplier, competing with the real next step, "Compila i
// listini" (fill in the price lists). The threshold is signaled by the
// card's own color (`is-below`) instead.
//
// The caption is one sentence stating what the command does; it doesn't
// restate the card's header two lines above, which already states the
// amount missing to reach the minimum order.
function renderSupplierMoveCommand(entry) {
  const belowThreshold = entry.minimumOrder > 0 && entry.total > 0 && entry.total < entry.minimumOrder;
  return `
    <div class="supplier-summary__move${belowThreshold ? " is-below" : ""}">
      <span class="supplier-summary__move-hint">Puoi portare questi prodotti su un altro fornitore mantenendo gli stessi colli.</span>
      <button class="button button--secondary" type="button" data-action="move-supplier" data-supplier-id="${escapeHtml(entry.supplier.id)}" data-focus-key="sposta-${escapeHtml(entry.supplier.id)}">
        Sposta tutto su un altro fornitore
      </button>
    </div>`;
}

function renderSupplierMoveOption(option, fromName) {
  const selected = option.id === state.supplierMove.choiceId;
  const delta = moveDeltaParts(option);
  const pieceTexts = movePieceTexts(option);
  const leftBehindText = moveLeftBehindText(option, fromName);
  // All threshold notes are shown, including the ones warning that a
  // threshold stays unmet: hiding those would leave only good news visible,
  // and the threshold is the reason for the move. Lost gifts are shown
  // alongside them, since they're a consequence of the same threshold.
  const notes = [...moveGiftNotes(option), ...moveThresholdNotes(option)];
  return `
    <label class="move-option${selected ? " is-selected" : ""}">
      <input class="move-option__radio" type="radio" name="supplier-move-option" value="${escapeHtml(option.id)}"
        data-move-option="${escapeHtml(option.id)}" data-focus-key="move-option-${escapeHtml(option.id)}" ${selected ? "checked" : ""}>
      <span class="move-option__body">
        <span class="move-option__title">${escapeHtml(option.label)}</span>
        <span class="move-option__meta">${formatInteger(option.movedCount)} ${option.movedCount === 1 ? "prodotto spostato" : "prodotti spostati"} su ${formatInteger(option.movableCount)}</span>
        <span class="move-option__goods">${escapeHtml(pieceTexts.goods)} · ${escapeHtml(pieceTexts.unit)}</span>
        ${leftBehindText ? `<span class="move-option__left">${escapeHtml(leftBehindText)}</span>` : ""}
        ${notes.map((note) => `<span class="move-option__note ${note.tone}">${escapeHtml(note.text)}</span>`).join("")}
      </span>
      <span class="move-option__delta ${delta.tone}">
        <strong>${escapeHtml(delta.text)}</strong>
        <small>${escapeHtml(delta.words)}</small>
      </span>
    </label>`;
}

function renderSupplierMoveDetails(option) {
  if (!option || !option.assignments.length) return "";
  const notes = [...moveGiftNotes(option), ...moveThresholdNotes(option)];
  const confirmations = option.assignments.filter((assignment) => assignment.needsConfirmation).length;
  return `
    <details class="move-details" ${apribile(`spostamento:${option.supplierId || option.id || ""}`)}>
      <summary>Vedi prodotto per prodotto</summary>
      <ul class="move-details__list">
        ${option.assignments.map((assignment) => {
          // A display is ordered by the display, not the carton: labeling it
          // "colli" here would repeat the same confusion the service's own
          // numbers just avoided.
          const unitaPlurale = findProduct(assignment.productId)?.orderUnitLabel || "colli";
          const unitaSingolare = orderUnitSingular(unitaPlurale);
          return `
          <li>
            <span class="move-details__name">${escapeHtml(assignment.productName)}</span>
            <span class="move-details__to">va a ${escapeHtml(supplierName(assignment.toSupplierId))}</span>
            <span class="move-details__prices">da ${formatEuro(assignment.previousLineNet)} a ${formatEuro(assignment.newLineNet)}</span>
            ${assignment.factorChanged ? `<span class="move-details__factor">Attenzione: a parità di ${escapeHtml(unitaPlurale)} si passa da ${formatInteger(assignment.previousFactor)} a ${formatInteger(assignment.newFactor)} pezzi per ${escapeHtml(unitaSingolare)}, cioè da ${formatInteger(assignment.previousPieces)} a ${formatInteger(assignment.newPieces)} pezzi consegnati.</span>` : ""}
            ${assignment.needsConfirmation ? '<span class="move-details__confirm">Questa riga chiede una conferma.</span>' : ""}
          </li>`;
        }).join("")}
      </ul>
      <p class="move-details__total">Differenza sull’ordine intero: <strong>${escapeHtml(moveDeltaParts(option).text)}</strong>.</p>
      ${notes.length ? `<ul class="move-details__notes">${notes.map((note) => `<li class="${note.tone}">${escapeHtml(note.text)}</li>`).join("")}</ul>` : ""}
      ${confirmations ? `<p class="move-details__confirm-count">${formatInteger(confirmations)} ${confirmations === 1 ? "prodotto avrà bisogno" : "prodotti avranno bisogno"} di una conferma nella pagina “Scegli prodotti e fornitori”.</p>` : ""}
    </details>`;
}

function renderSupplierMoveBody(fromName) {
  const move = state.supplierMove;
  if (move.loading) {
    return '<div class="loading-inline"><span class="spinner" aria-hidden="true"></span><span>Sto calcolando quanto costerebbe spostarli…</span></div>';
  }
  if (mode === "demo") {
    // In demo mode there's no local service to ask for a preview, and the
    // browser doesn't invent the numbers: it says so instead of showing fake
    // figures.
    return '<div class="empty-state empty-state--small"><div><strong>Non disponibile nell’esempio</strong><p>Le differenze di costo le calcolo sui listini veri: apri il comparatore senza l’esempio per usare questo comando.</p></div></div>';
  }
  if (move.error) {
    return renderAlert({ title: "Calcolo non riuscito", message: move.error, severity: "error", blocking: false });
  }
  const preview = move.preview;
  if (!preview) return "";
  if (!preview.options.length) {
    return `<div class="empty-state empty-state--small"><div><strong>Nessuna destinazione possibile</strong><p>Nessun altro fornitore ha i prodotti di ${escapeHtml(fromName)}: restano dove sono.</p></div></div>`;
  }
  const option = selectedSupplierMoveOption();
  return `
    <fieldset class="move-options">
      <legend class="visually-hidden">Dove spostare i prodotti di ${escapeHtml(fromName)}</legend>
      ${preview.options.map((entry) => renderSupplierMoveOption(entry, fromName)).join("")}
    </fieldset>
    <p class="move-dialog__hint">Le differenze sono sul totale netto di tutto l’ordine, non solo di ${escapeHtml(fromName)}. Il numero di colli non cambia.</p>
    ${renderSupplierMoveDetails(option)}`;
}

function renderSupplierMoveDialog() {
  const move = state.supplierMove;
  if (!move.from) return "";
  const preview = move.preview;
  const fromName = supplierMoveFromName();
  const countText = preview
    ? (preview.movableCount === 1 ? "il prodotto" : `i ${formatInteger(preview.movableCount)} prodotti`)
    : "i prodotti";
  const canApply = Boolean(selectedSupplierMoveOption());
  return `
    <div class="dialog-backdrop" role="presentation">
      <section class="move-dialog" role="dialog" aria-modal="true" aria-labelledby="move-title">
        <div class="move-dialog__header">
          <div>
            <h2 id="move-title">Sposta ${escapeHtml(countText)} di ${escapeHtml(fromName)} (${formatEuro(move.fromTotal)})</h2>
            <p>Scegli dove portarli. Niente viene cambiato finché non premi “Sposta i prodotti”.</p>
          </div>
          <button type="button" class="dialog-close" data-action="close-supplier-move" aria-label="Chiudi la finestra">×</button>
        </div>
        <div class="move-dialog__body" aria-live="polite">
          ${renderSupplierMoveBody(fromName)}
        </div>
        <div class="move-dialog__footer">
          <button type="button" class="button button--secondary" data-action="close-supplier-move">Annulla</button>
          <button type="button" class="button button--primary" data-action="apply-supplier-move" ${canApply ? "" : "disabled"}>Sposta i prodotti</button>
        </div>
      </section>
    </div>`;
}

// After a move: states what happened, whether a destination reached its
// threshold, and leaves a way to undo it.
function renderSupplierMoveUndo() {
  const undo = state.supplierMoveUndo;
  if (!undo) return "";
  const count = `${formatInteger(undo.movedCount)} ${undo.movedCount === 1 ? "prodotto" : "prodotti"}`;
  const verb = undo.movedCount === 1 ? "è passato" : "sono passati";
  const destination = undo.kind === "best" ? "al fornitore più conveniente per ciascun prodotto" : `a ${undo.label}`;
  return `
    <div class="undo-bar move-undo" role="status">
      <span class="move-undo__text">
        <strong>${escapeHtml(`${count} di ${undo.fromName} ${verb} ${destination}.`)}</strong>
        ${undo.notes.map((note) => `<span class="move-undo__note ${note.tone}">${escapeHtml(note.text)}</span>`).join("")}
        ${undo.confirmationCount ? `<span class="move-undo__note is-warn">${escapeHtml(`${formatInteger(undo.confirmationCount)} ${undo.confirmationCount === 1 ? "prodotto aspetta" : "prodotti aspettano"} una conferma nella pagina “Scegli prodotti e fornitori”.`)}</span>` : ""}
      </span>
      <button type="button" class="button button--secondary" data-action="undo-supplier-move">Rimetti i fornitori di prima</button>
    </div>`;
}

// Undo for the summary's "x" removal. Sits next to the supplier-move undo, at
// the top of page 3 rather than inside the list: the row the product just
// disappeared from is already gone, and a bar inserted into the list would
// scroll past what's being looked at. Fixed at the top, it doesn't push down
// the "Compila" button at the bottom.
function renderRimozioneUndo() {
  const undo = state.rimozioneUndo;
  if (!undo) return "";
  return `
    <div class="undo-bar" role="status">
      <span><strong>${escapeHtml(undo.name)}</strong> non è più nell’ordine: la quantità è stata azzerata.</span>
      <button type="button" class="button button--secondary" data-action="undo-remove-product">Rimetti nell’ordine</button>
    </div>`;
}

function safeDownloadUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(String(value), window.location.origin);
    if (url.origin !== window.location.origin && !["http:", "https:"].includes(url.protocol)) return "";
    return url.href;
  } catch {
    return "";
  }
}

// `writerIssues` and `historyIssues` come from the local service and are
// shown separately from `message`: a compilation that discarded a copy, or
// that didn't make it into next week's orders to review, isn't a full
// success and shouldn't be presented as one.
function renderCompileIssues(titolo, voci) {
  const elenco = asArray(voci).map((voce) => userFacingText(voce)).filter(Boolean);
  if (!elenco.length) return "";
  return `
    <div class="results__issues-block">
      <strong>${escapeHtml(titolo)}</strong>
      <ul class="results__issues">${elenco.map((voce) => `<li>${escapeHtml(voce)}</li>`).join("")}</ul>
    </div>`;
}

// A failed compilation. The choices are already saved and the retry button
// above genuinely retries, so the message can say so without overstating.
function renderCompileFailure() {
  const guasto = state.compileFailure;
  if (!guasto) return "";
  const controlli = asArray(guasto.controlli);
  return `
    <div class="results results--failed" role="alert">
      <strong>${escapeHtml(guasto.message)}</strong>
      ${controlli.length ? `
        <p class="results__issues-intro">${formatInteger(controlli.length)} ${controlli.length === 1 ? "prodotto è fermo" : "prodotti sono fermi"}, uno per riga:</p>
        <ul class="results__issues">
          ${controlli.map((voce) => `<li>${escapeHtml(voce.productName || voce.productId)}${voce.supplierName ? ` — ${escapeHtml(voce.supplierName)}` : ""}</li>`).join("")}
        </ul>` : ""}
      ${guasto.detail ? `<details class="results__detail" ${apribile("risposta-del-programma")}><summary>Che cosa dice il guasto</summary><p>${escapeHtml(guasto.detail)}</p></details>` : ""}
    </div>`;
}

// A disabled button with no other feedback for several seconds reads as
// broken rather than busy, and invites a second press.
//
// The bar is deliberately indeterminate, not a shortfall: the actual write
// runs in a Node process (`scripts/write_supplier_orders.mjs`) that reports
// no progress, so a percentage here would be fabricated. What the operator
// needs is three things: it's working, don't close the window, don't press
// again. The message says exactly that, plus where the result will appear.
//
// The recompute pipeline has a real percentage (`avanzamento.percento`)
// because it counts phases. If the writer ever reports its own progress,
// this bar can switch to that; until then it stays indeterminate.
function renderCompileProgress(quante) {
  if (!state.compiling) return "";
  return `
    <div class="compile-progress">
      <div class="compile-progress__bar" role="progressbar" aria-label="Compilazione dei listini in corso"></div>
      ${/* For a reduced-motion preference, the bar itself doesn't animate, so
           this sentence is the only signal and has to carry it alone. */ ""}
      <p class="compile-progress__nota" role="status">Sto scrivendo ${escapeHtml(contati(quante, "copia", "copie"))} del listino. Non chiudere il programma: quando ho finito te lo dico qui sotto.</p>
    </div>`;
}

function renderCompileResult() {
  if (!state.compileResult) return renderCompileFailure();
  const outputs = asArray(state.compileResult.outputs);
  // A successful compilation has no per-supplier errors: either everything
  // compiles, or `run_writer` stops and the response is a failure (see
  // `renderCompileFailure`). What actually arrives here is `writerIssues`.
  const writerIssues = asArray(state.compileResult.writerIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  // Delivery warnings: the document exists and is correct, but something
  // around it didn't go as planned — usually the human-readable name that
  // couldn't be set because the file was open.
  const deliveryIssues = asArray(state.compileResult.deliveryIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  const historyIssues = asArray(state.compileResult.historyIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  const parziale = Boolean(writerIssues.length || deliveryIssues.length || historyIssues.length);
  // The delivery button only appears once the local service sends a zip:
  // with no price lists there's nothing to deliver, and a button that
  // downloads a 404 is worse than no button. It's an `<a download>`, not a
  // `<button>`: it works without further JavaScript, and the URL is visible
  // in the status bar before it's clicked.
  const zipUrl = safeDownloadUrl(state.compileResult.zipUrl);
  const titolo = parziale
    ? "I listini sono stati preparati, ma non tutto è andato a buon fine."
    : String(state.compileResult.message || "Listini compilati correttamente.");
  return `
    <div class="results${parziale ? " results--partial" : ""}" role="status">
      <strong>${escapeHtml(titolo)}</strong>
      ${parziale && state.compileResult.message ? `<p class="results__original">${escapeHtml(String(state.compileResult.message))}</p>` : ""}
      ${renderCompileIssues("Listini non preparati o da ricontrollare", writerIssues)}
      ${renderCompileIssues("Documenti consegnati, ma con qualcosa da sapere", deliveryIssues)}
      ${renderCompileIssues("Non entra fra gli ordini da controllare la prossima settimana", historyIssues)}
      ${zipUrl ? `<p class="results__consegna"><a class="button button--${parziale ? "secondary" : "primary"}" href="${escapeHtml(zipUrl)}" download>Scarica i listini pronti per l’invio</a><span class="results__zip-nome">${escapeHtml(state.compileResult.zipNome || "")}</span></p>` : ""}
      ${outputs.length ? `<ul>${outputs.map((output) => {
        const url = safeDownloadUrl(output.url);
        return `<li>${url ? `<a href="${escapeHtml(url)}" download>${escapeHtml(output.name || "Scarica documento")}</a>` : escapeHtml(output.name || "Documento generato")}</li>`;
      }).join("")}</ul>` : ""}
    </div>`;
}

// States the audit records. A value missing from this table is shown as-is:
// inventing a translation would hide a contract change instead of
// surfacing it.
const STATI_COMPILAZIONE = {
  FILES_READY: "listini pronti",
  PLAN_READY: "solo il piano, nessun listino",
  SCONOSCIUTO: "stato non registrato",
};

const TIPI_FILE_COMPILAZIONE = {
  listino: "listino pronto",
  // Without this entry the file would list with no label, and a sheet named
  // "Prodotti da reperire" among price lists could be mistaken for one —
  // i.e. for something to send to a supplier.
  da_reperire: "prodotti da cercare altrove",
  piano: "piano dell’ordine",
  altro: "altro documento",
};

// The detail line under the date. The numbers come from the local service
// and are printed as-is: the browser recomputes neither totals nor row
// counts.
function compilazioneMeta(voce) {
  const parti = [];
  const righe = voce?.righe;
  if (Number.isFinite(righe)) parti.push(`${formatInteger(righe)} ${righe === 1 ? "riga" : "righe"}`);
  const listini = voce?.listini;
  if (Number.isFinite(listini)) parti.push(`${formatInteger(listini)} ${listini === 1 ? "listino" : "listini"}`);
  const stato = String(voce?.stato || "");
  if (stato) parti.push(STATI_COMPILAZIONE[stato] || stato);
  return parti.join(" · ");
}

function renderCompilazioneFile(documento) {
  const nome = String(documento?.nome || "Documento senza nome");
  const tipo = TIPI_FILE_COMPILAZIONE[String(documento?.tipo || "")] || "";
  const url = safeDownloadUrl(documento?.url);
  return `<li>${url ? `<a href="${escapeHtml(url)}" download>${escapeHtml(nome)}</a>` : escapeHtml(nome)}${tipo ? `<span class="compilazione__tipo">${escapeHtml(tipo)}</span>` : ""}</li>`;
}

// One compilation in the list. An entry with `completa: false` — its audit
// couldn't be read — is still shown, and says so: the folder exists, its
// files can be downloaded, and hiding it would make real price lists
// disappear from view.
function renderCompilazione(voce) {
  const cartella = String(voce?.cartella || "");
  const etichetta = String(voce?.etichetta || cartella || "Compilazione senza data");
  const completa = voce?.completa !== false;
  const totale = voce?.totaleNetto;
  const fornitori = asArray(voce?.fornitori);
  const documenti = asArray(voce?.file);
  const zipNome = String(voce?.zipNome || "");
  const zipUrl = safeDownloadUrl(voce?.zipUrl);
  // Documents the compilation produced that are missing from its folder:
  // almost always because the user moved them to attach to an email. Stated
  // anyway, or someone looking for a two-week-old price list can't tell
  // whether they moved it or the program lost it.
  const mancanti = asArray(voce?.mancanti);
  const confirming = state.compilazioni.confermaElimina === cartella;
  const deleting = state.compilazioni.eliminando === cartella;
  return `
    <li class="compilazione${completa ? "" : " is-incompleta"}">
      <div class="compilazione__head">
        <div>
          <strong>${escapeHtml(etichetta)}</strong>
          <span class="compilazione__meta">${escapeHtml(compilazioneMeta(voce))}</span>
        </div>
        ${completa
          ? renderBadge(totale == null ? "Totale non disponibile" : `Totale ${formatEuro(totale)}`, "info")
          : renderBadge("Dettagli non leggibili", "warning")}
      </div>
      ${completa ? "" : `<p class="compilazione__nota">I dettagli di questa compilazione non si leggono: la cartella <strong>${escapeHtml(cartella)}</strong> c’è e i suoi documenti si scaricano lo stesso, ma fornitori, totale e numero di righe non si possono mostrare.</p>`}
      ${fornitori.length ? `<ul class="compilazione__fornitori">${fornitori.map((fornitore) => `<li><span>${escapeHtml(fornitore?.nome || fornitore?.id || "Fornitore")}</span><strong>${fornitore?.totaleNetto == null ? "—" : formatEuro(fornitore.totaleNetto)}</strong></li>`).join("")}</ul>` : ""}
      ${zipUrl ? `<p class="compilazione__consegna"><a class="button button--secondary" href="${escapeHtml(zipUrl)}" download>Scarica i listini di questa compilazione</a><span class="compilazione__zip-nome">${escapeHtml(zipNome)}</span></p>` : ""}
      ${documenti.length ? `<ul class="compilazione__file">${documenti.map(renderCompilazioneFile).join("")}</ul>` : ""}
      ${mancanti.length ? `<p class="compilazione__mancanti">${escapeHtml(`${mancanti.length === 1 ? "Un documento di questa compilazione non è più nella sua cartella" : `${formatInteger(mancanti.length)} documenti di questa compilazione non sono più nella loro cartella`}: ${mancanti.join(", ")}. Se li hai spostati tu va bene; il resto si scarica lo stesso.`)}</p>` : ""}
      <div class="compilazione__actions">
        ${confirming ? `
          <span class="compilazione__confirm">Elimino gli ordini del ${escapeHtml(etichetta)}? Spariscono i listini già preparati e la domanda «è arrivata la merce?» per quei fornitori. Non si può annullare.</span>
          <button class="button button--danger-soft" type="button" data-action="confirm-delete-compilation" data-cartella="${escapeHtml(cartella)}" ${deleting ? "disabled" : ""}>${deleting ? "Eliminazione…" : "Elimina"}</button>
          <button class="button button--ghost" type="button" data-action="cancel-delete-compilation" ${deleting ? "disabled" : ""}>Annulla</button>
        ` : `<button class="button button--ghost" type="button" data-action="ask-delete-compilation" data-cartella="${escapeHtml(cartella)}">Elimina questi ordini</button>`}
      </div>
    </li>`;
}

// Step 3: compilations already made, most recent first. The order comes from
// the local service, which reads the dates on disk.
function renderCompilazioniPrecedenti() {
  if (mode === "demo") return "";
  const storico = state.compilazioni;
  const voci = asArray(storico.elenco);
  // Until the first response arrives, the list is empty because nothing has
  // read it yet, not because there are no compilations: saying "no
  // compilation recorded" to someone who just made ten would be wrong for as
  // long as the request takes.
  const inAttesa = storico.inCorso || (!storico.caricate && !storico.errore);
  return `
    <details class="panel compilazioni-panel" data-compilazioni ${storico.aperta ? "open" : ""}>
      <summary class="compilazioni-panel__summary">
        <strong>Compilazioni precedenti</strong>
        ${inAttesa ? renderBadge("Caricamento…", "neutral") : renderBadge(`${formatInteger(voci.length)} ${voci.length === 1 ? "compilazione" : "compilazioni"}`, voci.length ? "info" : "neutral")}
      </summary>
      <div class="compilazioni-panel__body">
        ${storico.errore ? renderAlert({
          title: "Storico non caricato",
          message: `${storico.errore} I documenti restano nelle loro cartelle e il resto di questa pagina funziona lo stesso.`,
          severity: "warning",
          blocking: false,
        }) : ""}
        ${voci.length ? `<ul class="compilazioni">${voci.map(renderCompilazione).join("")}</ul>` : ""}
        ${!voci.length && !storico.errore && !inAttesa ? '<div class="empty-state empty-state--small"><div><strong>Nessuna compilazione registrata</strong></div></div>' : ""}
      </div>
    </details>`;
}

function filteredSummaryProducts() {
  let products = orderedProducts().map((product) => {
    const offer = selectedOffer(product);
    const calculation = offer ? offerCalculation(product, offer) : null;
    return { product, offer, calculation, subtotal: calculation?.total || 0 };
  });
  if (state.summary.supplierId !== "all") {
    products = products.filter(({ offer }) => offer?.supplierId === state.summary.supplierId);
  }
  return products.sort((a, b) => {
    if (state.summary.sort === "total") return b.subtotal - a.subtotal;
    return a.product.name.localeCompare(b.product.name, "it", { sensitivity: "base" });
  });
}

function renderSummaryToolbar(activeTotals) {
  return `
    <div class="summary-toolbar">
      <fieldset class="segmented-control">
        <legend>Raggruppa il riepilogo</legend>
        <button type="button" data-action="summary-group" data-summary-group="supplier" class="${state.summary.grouping === "supplier" ? "is-active" : ""}" aria-pressed="${state.summary.grouping === "supplier"}">Per fornitore</button>
        <button type="button" data-action="summary-group" data-summary-group="product" class="${state.summary.grouping === "product" ? "is-active" : ""}" aria-pressed="${state.summary.grouping === "product"}">Per prodotto</button>
      </fieldset>
      <div class="field">
        <label for="summary-supplier">Fornitore</label>
        <select id="summary-supplier" class="select" data-summary-filter="supplierId">
          <option value="all">Tutti i fornitori</option>
          ${activeTotals.map((entry) => `<option value="${escapeHtml(entry.supplier.id)}" ${state.summary.supplierId === entry.supplier.id ? "selected" : ""}>${escapeHtml(entry.supplier.name)}</option>`).join("")}
        </select>
      </div>
      <div class="field">
        <label for="summary-sort">Ordina le righe</label>
        <select id="summary-sort" class="select" data-summary-filter="sort">
          <option value="name" ${state.summary.sort === "name" ? "selected" : ""}>Per nome del prodotto</option>
          <option value="total" ${state.summary.sort === "total" ? "selected" : ""}>Dal totale più alto</option>
        </select>
      </div>
    </div>`;
}

function renderSummaryQuantityControls(product) {
  const quantity = orderQuantity(product);
  return `
    <span class="summary-quantity" role="group" aria-label="Quantità di ${escapeHtml(product.name)}">
      <span class="summary-quantity__gruppo">
        <button type="button" data-action="change-quantity" data-product-id="${escapeHtml(product.id)}" data-delta="-1" data-focus-key="riepilogo-meno-${escapeHtml(product.id)}" aria-label="Diminuisci ${escapeHtml(product.name)}">−</button>
        <strong>${formatInteger(quantity)}</strong>
        <button type="button" data-action="change-quantity" data-product-id="${escapeHtml(product.id)}" data-delta="1" data-focus-key="riepilogo-piu-${escapeHtml(product.id)}" aria-label="Aumenta ${escapeHtml(product.name)}">+</button>
      </span>
      <button class="summary-quantity__remove" type="button" data-action="summary-remove-product" data-product-id="${escapeHtml(product.id)}" data-focus-key="riepilogo-togli-${escapeHtml(product.id)}" aria-label="Togli ${escapeHtml(product.name)} dall’ordine">×</button>
    </span>`;
}

function renderProductSummary() {
  const products = filteredSummaryProducts();
  if (!products.length) {
    return '<div class="empty-state"><div><strong>Nessun prodotto nel filtro scelto</strong><p>Seleziona un altro fornitore.</p></div></div>';
  }
  return `
    <article class="supplier-summary">
      <div class="supplier-summary__head">
        <div><strong>Prodotti da ordinare</strong><span>${products.length} ${products.length === 1 ? "riga visualizzata" : "righe visualizzate"}</span></div>
        <div class="supplier-summary__total"><strong>${formatEuro(products.reduce((sum, item) => sum + item.subtotal, 0))}</strong><span>subtotale visualizzato</span></div>
      </div>
      <ul class="order-lines order-lines--product">
        <li class="order-lines__intestazione" aria-hidden="true"><span>Prodotto</span><span>Fornitore</span><span>Quantità</span><span></span><span>Prezzo al pezzo</span><span>Totale</span></li>
        ${products.map(({ product, offer, calculation, subtotal }) => `
          <li>
            <span class="order-lines__name">${escapeHtml(product.name)}${offer ? `<small class="order-lines__ean">EAN fornitore: ${escapeHtml(offer.ean || "non presente")}</small>` : ""}${offer?.promotion ? `<small>Offerta: ${escapeHtml(offer.promotion)}</small>` : ""}</span>
            <span class="order-lines__supplier">${offer ? escapeHtml(offer.supplierName) : "Fornitore da scegliere"}</span>
            <span class="order-lines__qty">${escapeHtml(`${formatInteger(orderQuantity(product))} ${orderQuantity(product) === 1 ? orderUnitSingular(product.orderUnitLabel) : product.orderUnitLabel}`)}</span>
            ${renderSummaryQuantityControls(product)}
            ${renderPrezzoAlPezzo(offer ? calculation : null)}
            <strong>${formatEuro(subtotal)}</strong>
          </li>`).join("")}
      </ul>
    </article>`;
}

function renderPromotionSummary() {
  const promotions = asArray(state.review?.promotions);
  if (!promotions.length) return "";
  const counts = state.review?.promotionSummary?.counts || {};
  const earned = Math.max(0, finiteNumber(counts.ottenuta, 0));
  const near = Math.max(0, finiteNumber(counts.vicina, 0));
  const review = Math.max(0, finiteNumber(counts.da_verificare, 0));
  return `
    <section class="panel promotion-summary-panel">
      <div class="panel__header">
        ${/* Three bare numbers would be three reasons to act with no way to act
             on them: "3 close" doesn't say which suppliers or lead anywhere.
             The useful version of the same information already lives on
             page 2, in "Offerte a portata di mano" with its "Mostra i
             prodotti" command. The line below points there. */ ""}
        <div><h3>Offerte, omaggi e campioncini</h3><p>Il conteggio segue le quantità e i fornitori che hai scelto. Gli omaggi non riducono il totale dell’ordine. Per quelle a cui manca poco torna a «Scegli prodotti e fornitori», riquadro «Offerte a portata di mano»: lì c’è il comando.</p></div>
        ${renderBadge(`${formatInteger(promotions.length)} condizioni lette`, "info")}
      </div>
      <div class="promotion-summary-grid">
        <div class="promotion-summary-item is-earned"><span>Ottenuti o già applicati</span><strong>${formatInteger(earned)}</strong></div>
        <div class="promotion-summary-item is-near"><span>Ci manca poco</span><strong>${formatInteger(near)}</strong></div>
        <div class="promotion-summary-item is-review"><span>Da verificare</span><strong>${formatInteger(review)}</strong></div>
      </div>
    </section>`;
}

function renderCompileStep() {
  const totals = supplierTotals();
  const activeTotals = totals.filter((entry) => entry.products.length);
  const visibleTotals = state.summary.supplierId === "all" ? activeTotals : activeTotals.filter((entry) => entry.supplier.id === state.summary.supplierId);
  const belowThreshold = activeTotals.filter((entry) => entry.minimumOrder > 0 && entry.total > 0 && entry.total < entry.minimumOrder);
  const issues = collectIssues();
  const blockers = issues.filter((issue) => issue.blocking);
  // Per-product notices already live on each product's card in page 2, next
  // to the button that answers them, and two summary lines here already
  // count them with a link to that filter. Repeating them one by one on this
  // page would add scrolling, not an action. Only order-wide notices stay
  // here — summaries, minimum-order thresholds, price-list rows read with
  // caveats — plus the blockers, which stay named product by product in the
  // panel above.
  const notices = issues.filter((issue) => !issue.blocking && !issue.productId);
  const hasOrders = orderedProducts().length > 0;
  // Compiling is blocked while the pipeline is recomputing. The service
  // rejects it, and the button has to say so beforehand: until phase 9
  // replaces the comparison, compiling would write the previous run's price
  // lists — last week's prices — while page 1 shows an update in progress.
  const ricalcoloInCorso = pipelineInCorso();
  const canCompile = hasOrders
    && blockers.length === 0
    && (belowThreshold.length === 0 || state.acceptBelowThreshold)
    && !state.compiling
    && !ricalcoloInCorso;

  return `
    ${pageHeading("3. Riepilogo e compilazione", "Controlla prodotti, quantità, fornitori, subtotali e minimi d’ordine prima di creare i listini.")}
    ${/* Same sticky strip as page 2, same position: right under the heading.
         It matters more here — this is the page that decides whether the
         order ships — since supplier cards can run as tall as their product
         list, and without this the subtotals would scroll out of view. It's
         the same component, not a copy: a total printed in two places
         eventually shows two different numbers.
         Sticks at `top: calc(3rem + 4px)`, under the step bar. Nothing else
         on this page pins to the top, so the two never overlap; everything
         else just scrolls underneath. */ ""}
    ${renderStickySupplierTotals()}
    ${renderCambiamentoDocumenti()}
    ${renderSupplierMoveUndo()}
    ${renderRimozioneUndo()}
    ${blockers.length ? `<section class="panel"><div class="panel__header"><div><h3>Da risolvere prima di compilare</h3><p>${escapeHtml(contati(blockers.length, "cosa da sistemare", "cose da sistemare"))}</p></div>${renderBadge("Compilazione bloccata", "danger")}</div>${renderAvvisi(blockers, "bloccanti")}</section>` : ""}
    ${notices.length ? `<section class="panel"><div class="panel__header"><div><h3>Ricontrolla prima di compilare</h3><p>Non fermano niente, ma è l’ultimo momento per guardarli: dopo i listini sono scritti.</p></div>${renderBadge(contati(notices.length, "avviso", "avvisi"), "warning")}</div>${renderAvvisi(notices, "avvisi")}</section>` : ""}
    <section class="panel">
      <div class="panel__header">
        <div><h3>Ordini da preparare</h3><p>${orderedProducts().length} ${orderedProducts().length === 1 ? "prodotto assegnato" : "prodotti assegnati"} a ${activeTotals.length} ${activeTotals.length === 1 ? "fornitore" : "fornitori"}</p></div>
        ${renderBadge(`Totale ${formatEuro(allOrderTotal())}`, "info")}
      </div>
      ${renderSummaryToolbar(activeTotals)}
      ${activeTotals.length ? (state.summary.grouping === "supplier" ? visibleTotals.map(renderSupplierSummary).join("") : renderProductSummary()) : '<div class="empty-state"><div><strong>Nessun prodotto da ordinare</strong><p>Torna alla scelta dei prodotti e inserisci almeno una quantità.</p></div></div>'}
    </section>

    ${renderPromotionSummary()}

    <section class="compile-card compile-card--bottom">
      <div>
        ${/* The heading doesn't ask a question the button below already
             answers, and doesn't repeat "the original documents stay
             untouched" — already stated at the top of page 1. It states
             only what nothing else on the page says. */ ""}
        <h3>Compilazione dei listini</h3>
        <p>Creo una copia del listino di ogni fornitore, con le quantità che hai scelto. <strong class="no-send-message">Nessun ordine viene inviato.</strong></p>
      </div>
      <div class="compile-card__total">
        <span>Totale dell’ordine</span>
        <strong>${formatEuro(allOrderTotal())}</strong>
      </div>
      ${/* How many copies, and for whom. The only action on this page that
           leaves a mark outside the program, and before pressing it
           there's no way to know how many documents are about to exist.
           Just the count and names: per-supplier subtotals already live in
           the panel above, so they aren't repeated here. */ ""}
      ${activeTotals.length ? `<p class="compile-card__copie">${escapeHtml(`${activeTotals.length === 1 ? "Sarà creata" : "Saranno create"} ${contati(activeTotals.length, "copia", "copie")}: ${elencoNomi(activeTotals.map((entry) => entry.supplier.name))}.`)}</p>` : ""}
      ${belowThreshold.length ? `
        <label class="confirmation">
          <input type="checkbox" data-below-threshold-confirm data-focus-key="sotto-minimo" ${state.acceptBelowThreshold ? "checked" : ""}>
          <span><strong>Confermo gli ordini sotto il minimo.</strong> ${escapeHtml(belowThreshold.map((entry) => entry.supplier.name).join(", "))}: i listini saranno preparati anche se il minimo d’ordine non è raggiunto.</span>
        </label>` : ""}
      ${/* The button above ("Sposta tutto su un altro fornitore") is also blue
           and highlighted when a supplier is below threshold, but it isn't
           the next step — this is. This one uses the same blue as pages 1
           and 2, so "the primary action" reads the same way across all
           three. */ ""}
      ${/* Once the compilation succeeds, this button turns secondary rather
           than staying primary and highlighted above the result panel: the
           real next step is downloading the zip below, while this one only
           recompiles and creates another dated folder. Two primary actions
           at the point where the work ends would compete for attention. */ ""}
      <button class="button button--${state.compileResult ? "secondary" : "primary"} button--wide" type="button" data-action="compile" ${canCompile ? "" : "disabled"}>
        ${state.compiling ? "Compilazione in corso…" : ricalcoloInCorso ? "Il confronto si sta aggiornando…" : state.compileResult ? "Rifai i listini" : "Compila i listini"}
      </button>
      ${ricalcoloInCorso ? `<p class="compile-card__copie">Quando il confronto ha finito di aggiornarsi, il comando torna: adesso i listini nascerebbero con i prezzi di prima.</p>` : ""}
      ${/* Below the button, where the eye just was: above it would appear
           half a screen higher than where it was pressed. */ ""}
      ${renderCompileProgress(activeTotals.length)}
      ${renderCompileResult()}
    </section>
    ${renderCompilazioniPrecedenti()}
    <div class="page-actions">
      <button class="button button--secondary" type="button" data-action="previous">← Torna a prodotti e fornitori</button>
    </div>
    ${renderSupplierMoveDialog()}`;
}

// --- Pagina Impostazioni ---------------------------------------------------

// The settings changeable here, each with a human-readable label and what
// changing it does. Matches `VOCI_IMPOSTAZIONI` in the local service:
// anything not listed here isn't a preference but a fixed part of the
// program — the prompt version, for instance, is chosen by measuring how
// many wrong high-confidence acceptances it produces.
const LIMITI_AI = [
  {
    nome: "tetto_spesa_usd",
    etichetta: "Tetto di spesa per elaborazione (dollari)",
    passo: "0.5",
    minimo: "0",
    nota: "Superato il tetto la fase AI si ferma e i prodotti rimasti finiscono fra quelli da verificare a mano. Uno zero qui spegne la fase AI.",
  },
  {
    nome: "tetto_chiamate",
    etichetta: "Tetto di chiamate per elaborazione",
    passo: "100",
    minimo: "1",
    nota: "Il secondo freno, indipendente dalla spesa: conta le richieste partite, ripetizioni comprese.",
  },
  {
    nome: "parallelismo",
    etichetta: "Richieste in parallelo",
    passo: "1",
    minimo: "1",
    nota: "Quante domande partono insieme. Alzarlo accorcia l’attesa; troppo in alto il servizio risponde «rallenta» (429).",
  },
  {
    nome: "timeout_secondi",
    etichetta: "Attesa massima per risposta (secondi)",
    passo: "5",
    minimo: "1",
    nota: "Oltre questa attesa la singola domanda è considerata persa e il prodotto passa fra quelli da verificare.",
  },
];

function settingsValue(nome) {
  const valore = state.impostazioni.valori?.[nome];
  return valore === undefined || valore === null ? "" : String(valore);
}

function modelloConfigurato() {
  return String(state.impostazioni.valori?.model || "").trim();
}

function modelloNellElenco() {
  const identificativo = modelloConfigurato();
  if (!identificativo) return null;
  return state.impostazioni.modelli.elenco.find((voce) => voce.id === identificativo) || null;
}

// OpenRouter charges by the token; scaled to a per-million rate, the price
// becomes a number a person can actually compare.
function prezzoDelModello(modello) {
  const ingresso = finiteNumber(modello.prezzo_ingresso) * 1e6;
  const uscita = finiteNumber(modello.prezzo_uscita) * 1e6;
  if (!ingresso && !uscita) return "gratuito secondo il listino";
  return `${dollars.format(ingresso)} per milione di token in ingresso · ${dollars.format(uscita)} in uscita`;
}

function renderKeyStatus() {
  const chiave = state.impostazioni.chiave || {};
  if (!chiave.presente) return renderBadge("Nessuna chiave", "warning");
  const dove = chiave.origine === "ambiente" ? "dalla variabile d’ambiente" : "salvata sul computer";
  return renderBadge(`Chiave presente (…${chiave.coda}) · ${dove}`, "success");
}

function renderSettingsKeyPanel() {
  const chiave = state.impostazioni.chiave || {};
  const bloccato = state.impostazioni.salvandoChiave || state.impostazioni.provando;
  return `
    <section class="panel">
      <div class="panel__header">
        <div><h3>Chiave OpenRouter</h3><p>Serve alla fase che confronta le descrizioni con l’aiuto dell’AI. Senza chiave il programma lavora lo stesso, e i prodotti dubbi finiscono da verificare a mano.</p></div>
        ${renderKeyStatus()}
      </div>
      ${chiave.origine === "ambiente" ? renderAlert({
        title: "La chiave arriva dalla variabile d’ambiente",
        message: "OPENROUTER_API_KEY è impostata su questo computer e ha la precedenza: finché resta, una chiave salvata da questa pagina non verrebbe usata.",
        severity: "warning",
        blocking: false,
      }) : ""}
      <div class="field">
        <label for="impostazioni-chiave">Incolla la chiave</label>
        <input id="impostazioni-chiave" class="input" type="password" autocomplete="off" spellcheck="false"
               placeholder="${chiave.presente ? "Incolla una nuova chiave per sostituire quella attuale" : "sk-or-…"}"
               data-chiave-openrouter data-focus-key="impostazioni-chiave">
      </div>
      <p class="settings-note">
        La chiave viene salvata sul computer, in ${escapeHtml(state.impostazioni.percorsoChiave || "app/data/secrets.json")}, perché il programma deve poterla usare da solo la prossima volta.
        Dal servizio locale non torna mai indietro: di una chiave salvata questa pagina vede soltanto le ultime quattro lettere. Il campo si svuota appena la chiave è inviata.
      </p>
      ${/* Secondary, not primary: this page has two distinct save actions —
           this one and "Salva modello e limiti" at the bottom — and neither
           saves what the other does. A page has one primary action, and
           it's the one at the bottom, in the same place across all three
           pages of the workflow. Its label already says what it saves, so
           it doesn't change. */ ""}
      <div class="button-row">
        <button class="button button--secondary" type="button" data-action="salva-chiave" ${bloccato || !state.impostazioni.nuovaChiave ? "disabled" : ""}>
          ${state.impostazioni.salvandoChiave ? "Salvataggio…" : "Salva la chiave"}
        </button>
      </div>
    </section>`;
}

function renderSettingsModelPanel() {
  const elenco = state.impostazioni.modelli;
  const identificativo = modelloConfigurato();
  const scelto = modelloNellElenco();
  return `
    <section class="panel">
      <div class="panel__header">
        <div><h3>Modello</h3><p>Quale modello risponde alle domande sui prodotti dubbi.</p></div>
        ${elenco.caricamento ? renderBadge("Carico l’elenco…", "neutral") : renderBadge(`${formatInteger(elenco.elenco.length)} modelli nell’elenco`, elenco.elenco.length ? "info" : "warning")}
      </div>
      <div class="field">
        <label for="impostazioni-model">Identificativo del modello</label>
        <input id="impostazioni-model" class="input" type="text" autocomplete="off" spellcheck="false"
               value="${escapeHtml(identificativo)}" data-impostazione="model" data-focus-key="impostazioni-model">
      </div>
      <p class="settings-note">
        ${scelto
          ? `${escapeHtml(scelto.nome)} — ${escapeHtml(prezzoDelModello(scelto))}.`
          : identificativo
            ? "Questo identificativo non è nell’elenco: verrà usato così com’è. È il modo di usare un modello uscito dopo l’ultimo aggiornamento dell’elenco — verificalo con «Prova la connessione»."
            : "Scrivi l’identificativo del modello, o sceglilo dall’elenco qui sotto."}
      </p>
      <div class="field" style="margin-top: .8rem">
        <label for="impostazioni-elenco-modelli">Scegli dall’elenco di OpenRouter</label>
        <select id="impostazioni-elenco-modelli" class="select" data-elenco-modelli ${elenco.elenco.length ? "" : "disabled"}>
          <option value="">${elenco.caricamento ? "Carico l’elenco…" : elenco.elenco.length ? "Scegli un modello…" : "Elenco non disponibile"}</option>
          ${elenco.elenco.map((voce) => `
            <option value="${escapeHtml(voce.id)}" ${voce.id === identificativo ? "selected" : ""}>
              ${escapeHtml(voce.id)} — ${escapeHtml(prezzoDelModello(voce))}
            </option>`).join("")}
        </select>
      </div>
      ${elenco.errore ? renderAlert({
        title: "Elenco dei modelli non disponibile",
        message: `${elenco.errore} L’identificativo si può sempre scrivere a mano nel campo qui sopra.`,
        severity: "warning",
        blocking: false,
      }) : ""}
      <p class="settings-note">${escapeHtml(elenco.avviso || "")}</p>
      <div class="button-row">
        <button class="button button--ghost" type="button" data-action="ricarica-modelli" ${elenco.caricamento ? "disabled" : ""}>Aggiorna l’elenco</button>
      </div>
    </section>`;
}

function renderSettingsTestPanel() {
  const prova = state.impostazioni.prova;
  const usaQuellaIncollata = Boolean(state.impostazioni.nuovaChiave);
  return `
    <section class="panel">
      <div class="panel__header">
        <div><h3>Prova la connessione</h3><p>Una domanda vera su un prodotto finto: è l’unica cosa che dimostra insieme che la chiave vale, che il modello esiste e che risponde nel formato richiesto.</p></div>
        ${prova ? renderBadge(prova.stato, prova.ok ? "success" : prova.tono === "warning" ? "warning" : "danger") : ""}
      </div>
      <p class="settings-note">
        ${usaQuellaIncollata
          ? "Verrà provata la chiave che hai appena incollato qui sopra, senza salvarla: così una chiave sbagliata non sostituisce quella che funziona."
          : "Verrà provata la chiave già configurata."}
        La prova costa una chiamata sola, circa un decimillesimo di dollaro.
      </p>
      <div class="button-row">
        <button class="button button--secondary" type="button" data-action="prova-connessione" ${state.impostazioni.provando ? "disabled" : ""}>
          ${state.impostazioni.provando ? "Prova in corso…" : "Prova la connessione"}
        </button>
      </div>
      ${prova ? `
        ${renderAlert({
          title: prova.ok ? "Prova riuscita" : "Esito della prova",
          message: prova.messaggio,
          severity: prova.ok ? "success" : prova.tono === "warning" ? "warning" : "error",
          blocking: false,
        })}
        <p class="settings-note">
          Modello provato: ${escapeHtml(prova.modello || "")}${prova.dettaglio ? ` · ${escapeHtml(prova.dettaglio)}` : ""}
        </p>` : ""}
    </section>`;
}

function renderSettingsLimitsPanel() {
  return `
    <section class="panel">
      <div class="panel__header">
        <div><h3>Limiti della fase AI</h3><p>Quanto può spendere e quanto può aspettare, prima di lasciare il resto da verificare a mano.</p></div>
      </div>
      <div class="impostazioni-griglia">
        ${LIMITI_AI.map((voce) => `
          <div class="field">
            <label for="impostazioni-${escapeHtml(voce.nome)}">${escapeHtml(voce.etichetta)}</label>
            <input id="impostazioni-${escapeHtml(voce.nome)}" class="input" type="number" inputmode="decimal"
                   step="${escapeHtml(voce.passo)}" min="${escapeHtml(voce.minimo)}"
                   value="${escapeHtml(settingsValue(voce.nome))}"
                   data-impostazione="${escapeHtml(voce.nome)}" data-focus-key="impostazioni-${escapeHtml(voce.nome)}">
            <p class="settings-note">${escapeHtml(voce.nota)} Predefinito: ${escapeHtml(String(state.impostazioni.predefinite?.[voce.nome] ?? ""))}.</p>
          </div>`).join("")}
      </div>
    </section>`;
}

function renderSettingsPage() {
  if (mode === "demo") {
    return `
      <div class="page-heading">
        <div><h2>Impostazioni</h2><p>Non disponibili nell’esempio.</p></div>
        <div class="page-heading__meta"><button class="button button--secondary" type="button" data-action="chiudi-impostazioni">← Torna al lavoro</button></div>
      </div>
      <div class="empty-state"><div><strong>L’esempio non tocca le impostazioni</strong><p>Chiudi <code>?demo=1</code> e riapri il programma per cambiare chiave, modello e limiti.</p></div></div>`;
  }

  if (state.impostazioni.caricamento && !state.impostazioni.caricate) {
    return `
      <div class="loading-card">
        <span class="spinner" aria-hidden="true"></span>
        <div><strong>Leggo le impostazioni</strong><p>Chiave, modello e limiti della fase AI.</p></div>
      </div>`;
  }

  return `
    <div class="page-heading">
      <div>
        <h2>Impostazioni</h2>
        <p>La fase che confronta le descrizioni con l’aiuto dell’AI — tutto il resto del programma funziona anche senza — e le dichiarazioni che hai dato tu.</p>
      </div>
      <div class="page-heading__meta">
        <button class="button button--secondary" type="button" data-action="chiudi-impostazioni">← Torna al lavoro</button>
      </div>
    </div>
    ${state.impostazioni.errore ? renderAlert({ title: "Impostazioni", message: state.impostazioni.errore, severity: "error", blocking: false }) : ""}
    ${state.impostazioni.messaggio ? renderAlert({ title: "Impostazioni", message: state.impostazioni.messaggio, severity: "success", blocking: false }) : ""}
    ${renderSettingsKeyPanel()}
    ${renderSettingsModelPanel()}
    ${renderSettingsTestPanel()}
    ${renderSettingsLimitsPanel()}
    ${renderSettingsUguaglianzePanel()}
    ${renderSettingsConfermePanel()}
    <div class="page-actions">
      <button class="button button--secondary" type="button" data-action="chiudi-impostazioni">← Torna al lavoro</button>
      <button class="button button--primary" type="button" data-action="salva-impostazioni" ${state.impostazioni.salvando ? "disabled" : ""}>
        ${state.impostazioni.salvando ? "Salvataggio…" : "Salva modello e limiti"}
      </button>
    </div>`;
}

function rerenderPreservingFocus() {
  const active = document.activeElement;
  const key = active?.dataset?.focusKey;
  const selectionStart = active?.selectionStart;
  const selectionEnd = active?.selectionEnd;
  render();
  if (!key) return;
  const replacement = document.querySelector(`[data-focus-key="${CSS.escape(key)}"]`);
  if (!replacement) return;
  replacement.focus();
  if (typeof replacement.setSelectionRange === "function" && Number.isInteger(selectionStart)) {
    replacement.setSelectionRange(selectionStart, selectionEnd);
  }
}

// The two quantity-related undo bars — the summary's "x" removal and the
// bulk reset — can't stay available once the user has touched the numbers
// again: pressing them would overwrite what was just typed. Same reason
// `goToStep()` invalidates the supplier-move undo, as its own comment
// states.
function scadonoGliAnnulliDiQuantita() {
  state.rimozioneUndo = null;
  state.azzeramentoUndo = null;
}

function findProduct(id) {
  return state.review?.products.find((product) => product.id === id) || null;
}

function invalidateCompilationAcceptance() {
  state.acceptBelowThreshold = false;
  state.compileResult = null;
  state.compileFailure = null;
}

// The carton count stays the same when the supplier changes, but if the new
// supplier packs a different number of pieces per carton, the actual goods
// delivered change at the same typed carton count: this highlights the card
// for a few seconds and states it explicitly.
function flagSupplierFactorChange(product, previousOffer, newOffer) {
  const quantity = orderQuantity(product);
  const label = quantity === 1 ? orderUnitSingular(product.orderUnitLabel) : product.orderUnitLabel;
  state.supplierChangeNotices.set(product.id, {
    supplierName: newOffer.supplierName,
    quantityText: `${formatInteger(quantity)} ${label}`,
    previousFactor: previousOffer.quantityFactor,
    newFactor: newOffer.quantityFactor,
    previousPieces: quantity * previousOffer.quantityFactor,
    newPieces: quantity * newOffer.quantityFactor,
  });
  const existingTimer = state.supplierChangeTimers.get(product.id);
  if (existingTimer) window.clearTimeout(existingTimer);
  state.supplierChangeTimers.set(product.id, window.setTimeout(() => {
    state.supplierChangeNotices.delete(product.id);
    state.supplierChangeTimers.delete(product.id);
    render();
  }, 5000));
}

function goToStep(step) {
  const nextStep = Math.min(3, Math.max(1, finiteNumber(step, 1)));
  // Touching any step of the flow leaves the settings page, even without
  // pressing "Torna al lavoro": a pasted, unsaved key goes away with it.
  state.impostazioni.aperta = false;
  state.impostazioni.nuovaChiave = "";
  if (nextStep !== state.currentStep) {
    // Outside the summary page the move preview stops making sense, and
    // its undo can't stay available after manual changes: undoing it would
    // restore assignments the user has since changed.
    state.supplierMove = emptySupplierMove();
    state.supplierMoveUndo = null;
    scadonoGliAnnulliDiQuantita();
  }
  const cambiaPasso = nextStep !== state.currentStep;
  state.currentStep = nextStep;
  state.runtimeError = "";
  // Only when the step actually changes. Pressing the already-active step in
  // the bar — e.g. a mis-click on "Riepilogo e compilazione" while already
  // there — must not discard `compileResult`: that's what makes "Scarica i
  // listini" disappear, right when the download link to the just-created
  // documents is needed most.
  if (cambiaPasso) state.compileResult = null;
  scheduleSave();
  render();
  // Entering page 2 requests the orders still awaiting delivery; the
  // response arrives later and re-renders only if needed.
  if (nextStep === 2) loadPendingOrders();
  // Entering the summary page requests the list of compilations already
  // made: that's where a week-old price list is found again.
  if (nextStep === 3) loadCompilazioni();
  document.querySelector("#workspace")?.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function acceptedFile(file) {
  return /\.(xlsx|xls|csv)$/i.test(file.name);
}

// Why a document was rejected, stated to whoever dragged it in. The usual
// cause is the format, but a file with no extension has none to name, and
// "not a .xlsx" would answer a different question.
function motivoDelloScarto(nome) {
  const punto = String(nome || "").lastIndexOf(".");
  const estensione = punto > 0 ? String(nome).slice(punto).toLowerCase() : "";
  return estensione
    ? `${estensione} non è un formato che so leggere: servono .xlsx, .xls o .csv`
    : "senza estensione: non so di che formato è. Servono .xlsx, .xls o .csv";
}

function addPendingFiles(fileList, role = "suppliers") {
  // Adding documents during a recompute isn't a wait, it's a different
  // outcome: the drop is rejected just like the buttons are, with the same
  // message.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: i documenti si caricano appena ha finito.", "error");
    return;
  }
  const incoming = [...fileList];
  const rejected = incoming.filter((file) => !acceptedFile(file));
  if (rejected.length) {
    showToast(`${rejected.length === 1 ? "Ignorato" : "Ignorati"} ${contati(rejected.length, "documento", "documenti")}: il formato non è riconosciuto.`, "error");
    // The transient toast stays, but isn't the only record anymore: it faded
    // in under four seconds, never named the files, and anyone who missed it
    // ran the comparison without a supplier — i.e. paid more without
    // noticing. A rejection that isn't tracked isn't a choice, it's lost
    // data.
    const gia = new Set(state.fileScartati.map((voce) => voce.chiave));
    for (const file of rejected) {
      // Hidden files aren't listed. Dragging a folder from Finder also drags
      // in the `.DS_Store` macOS leaves behind: not a price list the user
      // meant to upload, and showing it among "rejected documents" with a
      // misleading reason ("no extension") would be noise on a line meant
      // to say only what matters.
      if (String(file.name || "").startsWith(".")) continue;
      const chiave = `${role}:${file.name}:${file.size}`;
      if (gia.has(chiave)) continue;
      gia.add(chiave);
      state.fileScartati.push({ chiave, role, nome: file.name, motivo: motivoDelloScarto(file.name) });
    }
  }
  const byKey = new Map(state.pendingFiles.map((entry) => [entry.key, entry]));
  for (const file of incoming.filter(acceptedFile)) {
    const key = `${role}:${file.name}:${file.size}:${file.lastModified}`;
    byKey.set(key, { file, role, key });
  }
  state.pendingFiles = [...byKey.values()];
  render();
}

async function fileToBase64(file) {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  const chunkSize = 0x8000;
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, Math.min(offset + chunkSize, bytes.length)));
  }
  return window.btoa(binary);
}

async function uploadFiles(role = "suppliers") {
  const selectedEntries = state.pendingFiles.filter((entry) => entry.role === role);
  if (!selectedEntries.length || state.uploading) return;
  // The button is already disabled during a recompute; this is the same
  // rule enforced where it matters, right before sending documents to the
  // local service.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: i documenti si caricano appena ha finito.", "error");
    return;
  }
  state.uploading = true;
  state.runtimeError = "";
  state.uploadMessage = "";
  render();

  try {
    if (mode === "demo") {
      await new Promise((resolve) => window.setTimeout(resolve, 450));
      const now = Date.now();
      const demoFiles = selectedEntries.map(({ file }, index) => ({
        id: `demo-upload-${now}-${index}`,
        name: file.name,
        kind: role === "management" ? "Gestionale" : "Listino",
        supplier: role === "management" ? "Gestionale" : "Fornitore riconosciuto",
        status: "ready",
        schemaState: "SCHEMA_NOTO",
        rows: 0,
        message: "Simulazione completata; il documento non è stato inviato.",
      }));
      state.review.files.push(...demoFiles);
      state.uploadMessage = `${demoFiles.length} documenti aggiunti alla simulazione.`;
    } else {
      const files = [];
      for (const { file } of selectedEntries) {
        files.push({ name: file.name, data: await fileToBase64(file), role });
      }
      // Save BEFORE the request, same reason as `abbinaLaRiga`: once the
      // upload finishes, the page rereads the comparison and REPLACES what's
      // in the page, and a quantity written moments ago — or one left behind
      // by a failed save — would go with it, silently producing a wrong
      // order. With clean state, `saveState()` returns immediately.
      const saved = await saveState();
      if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
      const result = await requestJson(API.upload, {
        method: "POST",
        body: JSON.stringify({ files }),
      });
      state.uploadMessage = String(result.message || `${files.length} documenti caricati. Analisi completata.`);
      const refreshed = await requestJson(API.review);
      state.review = normalizeReview(refreshed);
      restoreExcludedProducts();
      applyPipelineAfterInputChange(result.pipeline);
      state.acceptBelowThreshold = false;
    }
    const uploadedKeys = new Set(selectedEntries.map((entry) => entry.key));
    state.pendingFiles = state.pendingFiles.filter((entry) => !uploadedKeys.has(entry.key));
    showToast(state.uploadMessage || "Documenti caricati correttamente.");
  } catch (error) {
    state.runtimeError = `Caricamento non riuscito: ${error.message}`;
    showToast("Caricamento non riuscito. Controlla il messaggio in alto.", "error");
  } finally {
    state.uploading = false;
    render();
  }
}

// --------------------------------------------------------------------------
// Recomputing the comparison (page 1)
// --------------------------------------------------------------------------

// One second: the deterministic pipeline takes about 25 seconds in total and
// the AI phase a couple of minutes, so this is a poll rate that moves the
// bar without turning the requests into load.
const RITMO_PIPELINE_MS = 1000;
// How many consecutive failed status checks before saying so. One alone
// isn't news — the local service can be briefly busy — but five in a row,
// at the poll rate above, is five seconds of a stalled bar with no
// explanation.
const CONTROLLI_PERSI_PRIMA_DI_DIRLO = 5;

function resetSchemaMapping() {
  state.schemaMapping.loading = false;
  state.schemaMapping.loadedRunId = "";
  state.schemaMapping.data = null;
  state.schemaMapping.values = {};
  state.schemaMapping.valoriIniziali = "";
  state.schemaMapping.chiedendoUscita = false;
  state.schemaMapping.results = {};
  state.schemaMapping.error = "";
  state.schemaMapping.validating = false;
  state.schemaMapping.confirming = false;
  state.schemaMapping.documento = "";
}

function initializeSchemaMapping(payload) {
  const valori = {};
  for (const documento of payload.documents || []) {
    const proposta = documento.suggestion || {};
    const haFattore = Boolean(proposta.columns?.pieces_per_carton);
    valori[String(documento.profileId || "")] = {
      role: proposta.role === "master" ? "master" : "supplier",
      // No fallback to "__new__": when the service suggests no supplier, the
      // dropdown stays on "Scegli il fornitore…". Preselecting anything — a
      // real supplier or "Nuovo fornitore…" — would mean pressing Confirm
      // without looking still does something, silently mapping the file to
      // the wrong supplier.
      supplierChoice: String(proposta.supplierId || ""),
      supplierName: "",
      sheet: proposta.sheet || documento.sheets?.[0]?.name || "",
      headerRow: Number(proposta.headerRow || 1),
      dataStartRow: Number(proposta.dataStartRow || Number(proposta.headerRow || 1) + 1),
      // No row-break rule is chosen by default: the user confirms it by
      // looking at the preview row. A rule guessed by the program would be
      // worse than the number it replaces.
      dataStartBreak: "",
      markerText: "",
      columns: { ...(proposta.columns || {}) },
      orderColumn: proposta.orderColumn || "",
      factorMode: haFattore ? "column" : "fixed",
      piecesPerCartonDefault: 1,
      // Empty means "each row carries its own offer", the layout almost
      // every supplier uses: not a missing value but the default, and the
      // service treats it the same way. Guessing the layout from the file
      // would be worse than the value it replaces.
      commercialLayout: "",
      // The "Disponibilità" column's values are typed by the user after
      // looking at their price list; there's nothing to guess here, and the
      // field starts populated because the unsaved-work check compares
      // these same values against the current ones.
      availableValues: "",
    };
  }
  state.schemaMapping.data = payload;
  state.schemaMapping.loadedRunId = String(payload.runId || "");
  state.schemaMapping.values = valori;
  // The values as just loaded, so the app can tell whether leaving discards
  // something or is just going back.
  state.schemaMapping.valoriIniziali = JSON.stringify(valori);
  state.schemaMapping.chiedendoUscita = false;
  state.schemaMapping.results = {};
}

// Anyone who's touched anything inside "Rivedi le colonne" has unsaved work.
function colonneDaSalvare() {
  if (!state.schemaMapping.valoriIniziali) return false;
  return JSON.stringify(state.schemaMapping.values) !== state.schemaMapping.valoriIniziali;
}

async function loadSchemaMapping() {
  const runId = String(state.pipeline.stato?.runId || "");
  if (!schemaMappingRequired() || !runId || state.schemaMapping.loading) return;
  if (state.schemaMapping.loadedRunId === runId && state.schemaMapping.data) return;
  state.schemaMapping.loading = true;
  state.schemaMapping.error = "";
  render();
  try {
    const payload = await requestJson(API.schemasPending);
    initializeSchemaMapping(payload);
  } catch (error) {
    state.schemaMapping.error = error.message || "Non sono riuscito a preparare l’anteprima.";
  } finally {
    state.schemaMapping.loading = false;
    render();
  }
}

// --- Reviewing an already-uploaded price list's columns -------------------
// Same selector as the guided mapping; what differs is where the preview
// comes from and what confirming does. Nothing restarts here: the mapping
// becomes a saved decision, used by the next comparison, which the user
// triggers.

function colonneAperteAMano() {
  return Boolean(state.schemaMapping.documento);
}

async function apriColonneDocumento(nome) {
  if (!nome || state.schemaMapping.loading) return;
  resetSchemaMapping();
  state.schemaMapping.documento = nome;
  state.schemaMapping.loading = true;
  render();
  try {
    const payload = await requestJson(`${API.colonneDocumento}?nome=${encodeURIComponent(nome)}`);
    initializeSchemaMapping(payload);
    // After `initializeSchemaMapping`, which resets the state: without this
    // line the screen would close itself right after opening.
    state.schemaMapping.documento = nome;
  } catch (error) {
    state.schemaMapping.error = error.message || "Non sono riuscito ad aprire l’anteprima.";
  } finally {
    state.schemaMapping.loading = false;
    render();
  }
}

function chiudiColonneDocumento({ confermato = false } = {}) {
  // "Torna ai documenti" would otherwise silently discard everything set —
  // sheet, header row, columns filled in one by one by looking at the file.
  // When there's unsaved work, the confirmation happens here: one line,
  // like "Inizia nuova comparazione", not a dialog.
  if (!confermato && colonneDaSalvare()) {
    state.schemaMapping.chiedendoUscita = true;
    render();
    return;
  }
  resetSchemaMapping();
  render();
}

async function provaColonneDocumento() {
  if (state.schemaMapping.validating || state.schemaMapping.confirming) return;
  state.schemaMapping.validating = true;
  state.schemaMapping.error = "";
  state.schemaMapping.results = {};
  render();
  try {
    const risposta = await requestJson(API.colonneProva, {
      method: "POST",
      body: JSON.stringify({ ...schemaMappingPayload(), fileName: state.schemaMapping.documento }),
    });
    state.schemaMapping.results = Object.fromEntries(
      (risposta.documents || []).map((documento) => [String(documento.profileId || ""), documento]),
    );
  } catch (error) {
    state.schemaMapping.error = error.message || "Le colonne non sono corrette.";
  } finally {
    state.schemaMapping.validating = false;
    render();
  }
}

async function salvaColonneDocumento() {
  if (state.schemaMapping.validating || state.schemaMapping.confirming) return;
  state.schemaMapping.confirming = true;
  state.schemaMapping.error = "";
  render();
  try {
    // Save BEFORE the request, same reason as `abbinaLaRiga`: `loadReview()`
    // below rereads the comparison and REPLACES what's in the page, and a
    // quantity written moments ago — or one left behind by a failed save —
    // would go with it, silently producing a wrong order. With clean state,
    // `saveState()` returns immediately.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const risposta = await requestJson(API.colonneSalva, {
      method: "POST",
      body: JSON.stringify({ ...schemaMappingPayload(), fileName: state.schemaMapping.documento }),
    });
    resetSchemaMapping();
    applyPipelineAfterInputChange(risposta.pipeline);
    await loadReview();
  } catch (error) {
    state.schemaMapping.error = error.message || "Non sono riuscito a salvare le colonne.";
  } finally {
    state.schemaMapping.confirming = false;
    render();
  }
}

function schemaMappingPayload() {
  const documenti = state.schemaMapping.data?.documents || [];
  return {
    runId: state.schemaMapping.loadedRunId,
    mappings: documenti.map((documento) => {
      const valore = schemaDocumentValue(documento);
      const columns = { ...(valore.columns || {}) };
      let piecesPerCartonDefault;
      if (valore.factorMode === "fixed") {
        delete columns.pieces_per_carton;
        piecesPerCartonDefault = valore.piecesPerCartonDefault;
      }
      return {
        profileId: documento.profileId,
        role: valore.role,
        supplierId: valore.supplierChoice === "__new__" ? "" : valore.supplierChoice,
        supplierName: valore.supplierName,
        sheet: valore.sheet,
        headerRow: Number(valore.headerRow),
        dataStartRow: Number(valore.dataStartRow),
        dataStartMarker: schemaDataStartMarker(schemaSelectedSheet(documento, valore), valore),
        columns,
        orderColumn: valore.orderColumn ? Number(valore.orderColumn) : "",
        piecesPerCartonDefault,
        // Always sent, even when empty, for the same reason as the field
        // below: the service must be able to tell "the column exists but
        // you haven't said which values count" — which it rejects — from a
        // mapping where availability doesn't apply at all.
        availableValues: String(valore.availableValues || ""),
        // Always sent, even when empty: it declares that with no column it
        // produces nothing, and with a column it states how promotions are
        // written. Without this field the service wouldn't even look at the
        // chosen column, and picking one would have no effect.
        commercialConditions: { layout: String(valore.commercialLayout || "") },
      };
    }),
  };
}

async function validateSchemas() {
  if (state.schemaMapping.validating || state.schemaMapping.confirming) return;
  state.schemaMapping.validating = true;
  state.schemaMapping.error = "";
  state.schemaMapping.results = {};
  render();
  try {
    const risposta = await requestJson(API.schemasValidate, {
      method: "POST",
      body: JSON.stringify(schemaMappingPayload()),
    });
    state.schemaMapping.results = Object.fromEntries(
      (risposta.documents || []).map((documento) => [String(documento.profileId || ""), documento]),
    );
    showToast("Colonne controllate.");
  } catch (error) {
    state.schemaMapping.error = error.message || "Le colonne non sono corrette.";
    showToast("Controlla le colonne indicate.", "error");
  } finally {
    state.schemaMapping.validating = false;
    render();
  }
}

async function confirmSchemas() {
  if (state.schemaMapping.validating || state.schemaMapping.confirming) return;
  state.schemaMapping.confirming = true;
  state.schemaMapping.error = "";
  render();
  try {
    const risposta = await requestJson(API.schemasConfirm, {
      method: "POST",
      body: JSON.stringify(schemaMappingPayload()),
    });
    state.pipeline.stato = risposta.pipeline;
    state.pipeline.chiesto = true;
    state.schemaMapping.loadedRunId = "";
    state.schemaMapping.data = null;
    state.schemaMapping.results = {};
    showToast("Colonne confermate. Il confronto è ripartito.");
    render();
    pianificaControlloPipeline();
  } catch (error) {
    state.schemaMapping.error = error.message || "Non sono riuscito a confermare le colonne.";
    showToast("Conferma non riuscita.", "error");
  } finally {
    state.schemaMapping.confirming = false;
    render();
  }
}

function fermaPollingPipeline() {
  if (state.pipeline.timer) {
    window.clearTimeout(state.pipeline.timer);
    state.pipeline.timer = null;
  }
}

async function avviaRicalcolo() {
  if (pipelineInCorso()) return;
  // Also guards against a second click while the first request is still in
  // flight. A double click here is the natural reaction to a button that
  // shows no feedback for a second, and without this guard it would send
  // two requests: the service starts the first and answers 409 to the
  // second, showing "Il confronto non e' partito" while the comparison is
  // actually running.
  if (state.pipeline.avviando) return;
  state.pipeline.avviando = true;
  resetSchemaMapping();
  state.pipeline.errore = "";
  state.pipeline.chiesto = true;
  state.runtimeError = "";
  render();
  if (mode === "demo") {
    state.pipeline.avviando = false;
    state.pipeline.stato = {
      stato: "COMPLETATO",
      messaggio: "Nell’esempio il confronto è già pronto.",
      fasi: [],
      avanzamento: { fatte: 0, totali: 0, percento: 100 },
      numeri: {},
      avvisi: [],
    };
    render();
    return;
  }
  try {
    const risposta = await requestJson(API.pipelineAvvia, { method: "POST", body: JSON.stringify({}) });
    state.pipeline.stato = risposta;
    render();
    pianificaControlloPipeline();
  } catch (error) {
    state.pipeline.errore = error.message || "Il confronto non è partito.";
    showToast("Il confronto non è partito.", "error");
    render();
  } finally {
    state.pipeline.avviando = false;
  }
}

// Where the recompute stands, for anyone who can't see the bar. Writes into
// `#avanzamento-annuncio`, in `index.html`, which is static and never
// destroyed by `render()`: that's the only way `aria-live` actually works —
// an element that persists with text that changes. Writing the same text
// every time would repeat the announcement on every poll, so it only writes
// when the text changes.
function annunciaAvanzamento(stato) {
  const regione = document.querySelector("#avanzamento-annuncio");
  if (!regione) return;
  const fase = String(stato?.messaggio || "").replace(/…$/, "");
  const percento = Math.round(finiteNumber(stato?.avanzamento?.percento, 0));
  const frase = fase ? `Confronto al ${percento} per cento. ${fase}.` : `Confronto al ${percento} per cento.`;
  if (regione.textContent === frase) return;
  regione.textContent = frase;
}

function pianificaControlloPipeline() {
  fermaPollingPipeline();
  state.pipeline.timer = window.setTimeout(controllaPipeline, RITMO_PIPELINE_MS);
}

async function controllaPipeline() {
  state.pipeline.timer = null;
  let stato;
  try {
    stato = await requestJson(API.pipelineStato);
  } catch (error) {
    // A missed status check isn't a lost recompute: the pipeline runs in the
    // local service, not in the page. It retries on the next poll — but if
    // misses pile up, the bar would stay frozen at the last percentage
    // forever while the user waits on a recompute nobody can report on.
    // Past the threshold it says so, and keeps retrying regardless.
    state.pipeline.controlliPersi += 1;
    if (state.pipeline.controlliPersi >= CONTROLLI_PERSI_PRIMA_DI_DIRLO) {
      state.pipeline.contattoPerso = `L'avanzamento qui sotto è fermo a prima: non riesco più a sapere a che punto è (${error.message}). Il confronto continua per conto suo; riprovo da solo, e se non riparte chiudi e riapri il comparatore.`;
      render();
    }
    pianificaControlloPipeline();
    return;
  }
  state.pipeline.controlliPersi = 0;
  state.pipeline.contattoPerso = "";
  state.pipeline.stato = stato;
  if (String(stato.stato || "") === "IN_CORSO") {
    rerenderPreservingFocus();
    annunciaAvanzamento(stato);
    pianificaControlloPipeline();
    return;
  }
  if (String(stato.stato || "") === "COMPLETATO") {
    try {
      state.review = normalizeReview(await requestJson(API.review));
      restoreExcludedProducts();
      invalidateCompilationAcceptance();
      showToast(String(stato.messaggio || "Confronto aggiornato."));
    } catch (error) {
      state.runtimeError = `Il confronto è stato aggiornato ma la pagina non è riuscita a rileggerlo: ${error.message}`;
    }
  } else if (String(stato.stato || "") === "ERRORE") {
    // The message depends on why it stopped: when it's a column-mapping
    // issue there's a way to restart right away, and that's what gets said.
    if (schemaMappingRequired(stato)) {
      showToast("Un listino ha colonne che non conosco: indicamele qui sotto e riparto.", "error");
      loadSchemaMapping();
    } else {
      showToast("Il confronto non è riuscito: quello di prima è rimasto al suo posto.", "error");
    }
  }
  render();
}

// --- The four dialogs, one shared behavior ---------------------------------
// Four dialogs — the order column, the price-list viewer, the catalog, and
// the supplier move — all declare `aria-modal="true"`, so all four need to
// honor it the same way: Esc closes, focus moves in on open and back to the
// opener on close, and a click on the backdrop closes it too. This array is
// what makes that one shared behavior instead of four separate ones.
//
// The list order matches the markup, top dialog to bottom: `render()`
// appends the order column after any page's content, so it's always drawn
// last and closes first.
//
// `primiCampi` is a fallback chain, not a single selector: a dialog still
// loading has no field of its own yet, and focus falls back to its "×",
// which is inside the dialog either way.
const FINESTRE = [
  {
    aperta: () => Boolean(state.colonnaOrdine.aperta),
    chiudi: () => chiudiLaColonnaDOrdine(),
    primiCampi: ["#colonna-ordine-scelta", ".colonna-dialog .dialog-close"],
  },
  {
    aperta: () => Boolean(state.listino.aperto),
    chiudi: () => chiudiIlListino(),
    primiCampi: ["#listino-cerca", ".listino-dialog .dialog-close"],
  },
  {
    aperta: () => Boolean(state.catalog.open),
    chiudi: () => closeCatalog(),
    primiCampi: ["#catalog-search", ".catalog-dialog .dialog-close"],
  },
  {
    aperta: () => Boolean(state.supplierMove.from),
    chiudi: () => closeSupplierMove(),
    primiCampi: [".move-dialog [data-move-option]", ".move-dialog .dialog-close"],
  },
];

function finestraInCima() {
  return FINESTRE.find((finestra) => finestra.aperta()) || null;
}

// Who opened the dialog, to return focus there on close. Stores the
// opener's `data-focus-key`, not the node itself: the node gets destroyed
// by the next `render()`.
function ricordaChiApreLaFinestra() {
  state.fuocoPrimaDellaFinestra = String(document.activeElement?.dataset?.focusKey || "");
}

// `setTimeout(…, 0)`: the `autofocus` attribute isn't honored on nodes
// inserted via `innerHTML`, which is why the catalog needs this even though
// its markup already declares `autofocus`.
function portaIlFuocoDentro(selettori) {
  window.setTimeout(() => {
    for (const selettore of selettori) {
      const nodo = document.querySelector(selettore);
      if (nodo) {
        nodo.focus();
        return;
      }
    }
  }, 0);
}

// On close, focus returns to where it was. If the command that opened the
// dialog is gone by then — "Sfoglia il listino" after a manual match has
// changed the product's card — this falls back to `#workspace`, where
// `goToStep()` already sends focus on every page change.
function restituisciIlFuoco() {
  const chiave = state.fuocoPrimaDellaFinestra;
  if (chiave === null) return;
  state.fuocoPrimaDellaFinestra = null;
  window.setTimeout(() => {
    const comando = chiave ? document.querySelector(`[data-focus-key="${CSS.escape(chiave)}"]`) : null;
    comando?.focus({ preventScroll: true });
    // Being in the DOM doesn't mean it can take focus. The command can sit
    // inside a closed `<details>` — "Sfoglia il listino X" is inside the
    // comparison panel — so `querySelector` finds it, `focus()` does
    // nothing, and focus stays on `<body>`, restarting Tab from the top of
    // the page. This checks where focus actually landed, not where it was
    // sent.
    if (comando && document.activeElement === comando) return;
    document.querySelector("#workspace")?.focus({ preventScroll: true });
  }, 0);
}

// Both Esc and a click on the backdrop go through here.
function chiudiLaFinestraInCima() {
  const finestra = finestraInCima();
  if (!finestra) return false;
  // Focus return does NOT live here, it lives in each of the four close
  // functions. Esc, the "×" and a backdrop click all pass through here, but
  // the three primary actions — saving the order column, adding a catalog
  // product, applying the move — close their own dialog directly, and need
  // the same focus handling a keyboard user relies on.
  finestra.chiudi();
  return true;
}

function openCatalog() {
  ricordaChiApreLaFinestra();
  state.catalog.open = true;
  state.catalog.query = "";
  state.catalog.results = [];
  state.catalog.error = "";
  render();
  portaIlFuocoDentro(["#catalog-search", ".catalog-dialog .dialog-close"]);
}

function closeCatalog() {
  window.clearTimeout(state.catalog.timer);
  state.catalog.open = false;
  state.catalog.loading = false;
  state.catalog.error = "";
  render();
  restituisciIlFuoco();
}

function scheduleCatalogSearch() {
  window.clearTimeout(state.catalog.timer);
  const query = state.catalog.query.trim();
  if (query.length < 2) {
    state.catalog.loading = false;
    state.catalog.results = [];
    state.catalog.error = "";
    rerenderPreservingFocus();
    return;
  }
  state.catalog.loading = true;
  state.catalog.error = "";
  rerenderPreservingFocus();
  state.catalog.timer = window.setTimeout(() => searchCatalog(query), 350);
}

async function searchCatalog(query) {
  try {
    let products;
    if (mode === "demo") {
      products = demoCatalogProducts(query);
    } else {
      const result = await requestJson(`${API.productSearch}?${new URLSearchParams({ q: query, limit: "20" })}`);
      products = asArray(result.products ?? result.results);
    }
    if (query !== state.catalog.query.trim()) return;
    state.catalog.results = products.map(normalizeCatalogProduct);
    state.catalog.error = "";
  } catch (error) {
    if (query !== state.catalog.query.trim()) return;
    state.catalog.results = [];
    state.catalog.error = `La ricerca nel catalogo non è disponibile: ${error.message}`;
  } finally {
    if (query === state.catalog.query.trim()) {
      state.catalog.loading = false;
      rerenderPreservingFocus();
    }
  }
}

async function addCatalogProduct(productId) {
  const candidate = state.catalog.results.find((product) => product.id === productId);
  if (!candidate) return;
  state.catalog.loading = true;
  state.catalog.error = "";
  render();
  try {
    if (mode === "demo") {
      if (!state.review.products.some((product) => product.id === candidate.id)) {
        const offers = state.review.suppliers.map((supplier, index) => ({
          id: `${candidate.id}-${supplier.id}`,
          supplierId: supplier.id,
          supplierName: supplier.name,
          price: candidate.bestPrice + index * 0.35,
          orderUnitPriceNet: candidate.bestPrice + index * 0.35,
          unitsPerOrderUnit: 1,
          quantityFactor: 1,
          pricePerPiece: candidate.bestPrice + index * 0.35,
          available: true,
          matchStatus: "Prodotto del catalogo",
          compositionStatus: "",
          warning: "",
          promotion: "",
        }));
        state.review.products.push({
          ...candidate,
          itemType: "product",
          quantity: 0,
          suggestedQuantity: null,
          quantitySource: "utente",
          lastUnitPrice: null,
          orderUnitLabel: "colli",
          selectedSupplierId: offers[0]?.supplierId || "",
          confirmed: true,
          requiresConfirmation: false,
          confirmationMessage: "",
          components: [],
          warnings: [],
          offers,
          addedManually: true,
        });
      }
    } else {
      // Save BEFORE the request. Below, the comparison is reread from the
      // service and REPLACES what's in the page: a quantity written less
      // than 450 ms ago — or left behind by a failed save about to retry —
      // isn't on disk yet, and that reread would discard it, silently
      // persisting the stale number as the real order. With clean state,
      // `saveState()` returns immediately at no cost.
      if (!(await saveState())) throw new Error("Le modifiche non sono ancora salvate: riprova fra un momento.");
      const result = await requestJson(API.productAdd, {
        method: "POST",
        body: JSON.stringify({ catalogId: candidate.id }),
      });
      if (result.ok === false) throw new Error(result.message || "Il prodotto non è stato aggiunto.");
      const refreshed = result.review || await requestJson(API.review);
      state.review = normalizeReview(refreshed);
      restoreExcludedProducts();
    }
    state.excludedProductIds.delete(candidate.id);
    persistExcludedProducts();
    state.catalog.open = false;
    state.catalog.loading = false;
    restituisciIlFuoco();
    state.filters.search = candidate.name;
    state.filters.status = "all";
    state.filters.page = 1;
    invalidateCompilationAcceptance();
    scheduleSave();
    showToast(`${candidate.name} aggiunto all’elenco.`);
    render();
  } catch (error) {
    state.catalog.loading = false;
    state.catalog.error = `Aggiunta non riuscita: ${error.message}`;
    render();
  }
}

function excludeProduct(product) {
  state.exclusionUndo = {
    id: product.id,
    name: product.name,
    quantity: orderQuantity(product),
    selectedSupplierId: product.selectedSupplierId,
    confirmed: product.confirmed,
  };
  // Holds the same data as the undo bar, but doesn't expire: there's only
  // one bar, and the next exclusion replaces it.
  state.sceltePrimaDellEsclusione.set(product.id, {
    quantity: orderQuantity(product),
    selectedSupplierId: product.selectedSupplierId || "",
    confirmed: Boolean(product.confirmed),
  });
  product.quantity = 0;
  product.confirmed = false;
  state.excludedProductIds.add(product.id);
  persistExcludedProducts();
  invalidateCompilationAcceptance();
  scheduleSave();
  render();
}

function restoreProduct(product, previous = null) {
  state.excludedProductIds.delete(product.id);
  state.sceltePrimaDellEsclusione.delete(product.id);
  if (previous) {
    product.quantity = previous.quantity;
    product.selectedSupplierId = previous.selectedSupplierId;
    product.confirmed = previous.confirmed;
  }
  persistExcludedProducts();
  // Only when the undo bar is about THIS product: restoring one must not
  // clear the undo for a different product excluded right after it.
  if (state.exclusionUndo && state.exclusionUndo.id === product.id) state.exclusionUndo = null;
  invalidateCompilationAcceptance();
  scheduleSave();
  render();
}

// Asks the local service what it would cost to move all of a supplier's
// products elsewhere. Just a preview: nothing changes until the user
// confirms, and no price is computed here.
async function openSupplierMove(supplierId) {
  const entry = supplierTotals().find((candidate) => candidate.supplier.id === supplierId);
  if (!entry || !entry.products.length) return;
  ricordaChiApreLaFinestra();
  state.supplierMove = {
    ...emptySupplierMove(),
    from: supplierId,
    fromName: entry.supplier.name,
    fromTotal: entry.total,
    loading: mode !== "demo",
  };
  render();
  portaIlFuocoDentro([".move-dialog [data-move-option]", ".move-dialog .dialog-close"]);
  if (mode === "demo") return;

  const requested = supplierId;
  try {
    const payload = await requestJson(API.supplierMovePreview, {
      method: "POST",
      body: JSON.stringify({ ...snapshot(), from: supplierId }),
    });
    if (payload?.ok === false) throw new Error(payload.message || "Il servizio locale non ha calcolato le alternative.");
    // The dialog may have been closed or reopened on a different supplier
    // by now: a late response must not overwrite the right one.
    if (state.supplierMove.from !== requested) return;
    const preview = normalizeMovePreview(payload);
    state.supplierMove.preview = preview;
    // Default: the best alternative for each product.
    state.supplierMove.choiceId = (preview.options.find((option) => option.kind === "best") || preview.options[0])?.id || "";
  } catch (error) {
    if (state.supplierMove.from !== requested) return;
    state.supplierMove.error = `Non è stato possibile calcolare le alternative: ${error.message}`;
  } finally {
    if (state.supplierMove.from === requested) {
      state.supplierMove.loading = false;
      render();
    }
  }
}

function closeSupplierMove() {
  if (!state.supplierMove.from) return;
  state.supplierMove = emptySupplierMove();
  render();
  restituisciIlFuoco();
}

// Applies the choice: changes ONLY the supplier of the products named by
// the preview. Quantity, exclusion state and quantity origin stay as they
// are; confirmations don't, because a confirmation given for one offer
// doesn't carry over to another.
function applySupplierMove() {
  const option = selectedSupplierMoveOption();
  if (!option) return;
  const fromName = supplierMoveFromName();
  const previous = [];

  for (const assignment of option.assignments) {
    const product = findProduct(assignment.productId);
    if (!product) continue;
    previous.push({
      id: product.id,
      selectedSupplierId: product.selectedSupplierId,
      confirmed: Boolean(product.confirmed),
    });
    const previousOffer = selectedOffer(product);
    product.selectedSupplierId = assignment.toSupplierId;
    product.confirmed = false;
    const newOffer = selectedOffer(product);
    // Same carton count but a different pieces-per-carton: the card gets
    // highlighted the same way as a single supplier change.
    if (assignment.factorChanged && previousOffer && newOffer) {
      flagSupplierFactorChange(product, previousOffer, newOffer);
    }
  }

  if (!previous.length) {
    showToast("Nessun prodotto è stato spostato: l’elenco non corrisponde più.", "error");
    closeSupplierMove();
    return;
  }

  state.supplierMoveUndo = {
    fromName,
    kind: option.kind,
    label: option.label,
    movedCount: previous.length,
    notes: moveThresholdNotes(option),
    confirmationCount: option.assignments.filter((assignment) => assignment.needsConfirmation).length,
    previous,
  };
  // If the summary was filtered to the supplier just emptied out, its card
  // is gone: falls back to showing all suppliers, or the page would be
  // empty with no explanation.
  const emptied = state.supplierMove.from;
  if (state.summary.supplierId === emptied
    && !supplierTotals().some((entry) => entry.supplier.id === emptied && entry.products.length)) {
    state.summary.supplierId = "all";
  }

  invalidateIssues();
  invalidateCompilationAcceptance();
  scheduleSave();
  closeSupplierMove();
  showToast("Spostamento eseguito: puoi annullarlo dal riquadro in cima al riepilogo.");
  document.querySelector(".move-undo")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

// Restores exactly the assignments and confirmations from before the move,
// the same pattern as the exclusion undo.
function undoSupplierMove() {
  const undo = state.supplierMoveUndo;
  if (!undo) return;
  for (const entry of undo.previous) {
    const product = findProduct(entry.id);
    if (!product) continue;
    product.selectedSupplierId = entry.selectedSupplierId;
    product.confirmed = entry.confirmed;
    // The pieces-per-carton notices belonged to the move being undone.
    const timer = state.supplierChangeTimers.get(entry.id);
    if (timer) window.clearTimeout(timer);
    state.supplierChangeTimers.delete(entry.id);
    state.supplierChangeNotices.delete(entry.id);
  }
  state.supplierMoveUndo = null;
  invalidateIssues();
  invalidateCompilationAcceptance();
  scheduleSave();
  render();
  showToast("Spostamento annullato: i prodotti sono tornati al fornitore di prima.");
}

async function compileOrders() {
  if (state.compiling) return;
  invalidateIssues();
  const blockers = collectIssues().filter((issue) => issue.blocking);
  const belowThreshold = supplierTotals().filter((entry) => entry.minimumOrder > 0 && entry.total > 0 && entry.total < entry.minimumOrder);
  if (blockers.length) {
    showToast(
      blockers.length === 1
        ? "C’è 1 cosa da sistemare qui sopra, nel riquadro rosso."
        : `Ci sono ${formatInteger(blockers.length)} cose da sistemare qui sopra, nel riquadro rosso.`,
      "error",
    );
    return;
  }
  if (!orderedProducts().length) {
    showToast("Non c’è niente da ordinare: torna alla pagina 2 e metti almeno una quantità.", "error");
    return;
  }
  if (belowThreshold.length && !state.acceptBelowThreshold) {
    showToast("Spunta «Confermo gli ordini sotto il minimo» qui sopra per andare avanti.", "error");
    return;
  }

  state.compiling = true;
  state.runtimeError = "";
  state.compileResult = null;
  state.compileFailure = null;
  render();

  try {
    if (mode === "demo") {
      await new Promise((resolve) => window.setTimeout(resolve, 650));
      state.compileResult = {
        ok: true,
        message: "Esempio completato: non sono stati creati documenti reali.",
        outputs: [
          { name: "Ordine BETULLA compilato.xlsx" },
          { name: "Ordine Larice compilato.xlsx" },
          { name: "Ordine NOCE compilato.xls" },
        ],
      };
    } else {
      const saved = await saveState();
      if (!saved) throw new Error("Correggi l'errore di salvataggio prima di compilare.");
      const result = await requestJson(API.compile, {
        method: "POST",
        body: JSON.stringify(snapshot()),
      });
      if (result.ok === false) throw new Error(result.message || "Il servizio locale non ha compilato i listini.");
      // Compiling rewrites the whole state, and without the new version the
      // `scheduleSave()` below would be rejected; `requestJson` already
      // captures it for every call, so there's nothing to do here.
      state.compileResult = result;
    }
    state.currentStep = 3;
    scheduleSave();
    // The compilation just made is the newest entry in the history: without
    // this request, the "Compilazioni precedenti" panel would be one behind
    // and look like the new folder was never created.
    loadCompilazioni();
    // And the panel opens. `state.compilazioni.aperta` starts `false`, so
    // without this the panel that says WHERE the just-created documents
    // went would stay closed, one click away with nothing pointing to it —
    // right when a successful compilation is exactly the information being
    // sought.
    state.compilazioni.aperta = true;
    showToast(state.compileResult.message || "Listini compilati correttamente.");
  } catch (error) {
    // The choices stay in `state.review` and are already saved: the
    // "Compila i listini" button below is a genuine retry, so the message
    // can promise that. The technical detail isn't lost, it goes into the
    // collapsible.
    state.compileFailure = {
      message: "I listini non sono stati preparati. Le tue scelte sono salvate: puoi riprovare col pulsante qui sotto.",
      detail: String(error.message || ""),
      controlli: asArray(error.dettagli?.errors).map(normalizeIssue).filter((voce) => voce.productName || voce.productId),
    };
    // `state.runtimeError` is left untouched: if the save had failed, that
    // message is still true, and overwriting it would make it disappear.
    showToast("I listini non sono stati preparati. Nessun ordine è stato inviato.", "error");
  } finally {
    state.compiling = false;
    render();
  }
}

// --- Impostazioni: le richieste al servizio locale -------------------------

function openSettings() {
  state.impostazioni.aperta = true;
  state.impostazioni.errore = "";
  state.impostazioni.messaggio = "";
  state.impostazioni.prova = null;
  render();
  if (mode === "demo") return;
  loadSettings();
  if (!state.impostazioni.modelli.caricati) loadModels();
  // Reloaded on every open, not just once: a declaration can be created
  // within this same session, by manually matching a price-list row, and a
  // list frozen at first open wouldn't show it.
  caricaLeUguaglianze({ query: "" });
}

function closeSettings() {
  state.impostazioni.aperta = false;
  // A pasted, unsaved key doesn't survive leaving the page: keeping it in
  // memory would serve no purpose, just a loose value lying around.
  state.impostazioni.nuovaChiave = "";
  state.impostazioni.messaggio = "";
  render();
  document.querySelector("#workspace")?.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function loadSettings() {
  if (state.impostazioni.caricamento) return;
  state.impostazioni.caricamento = true;
  render();
  try {
    const payload = await requestJson(API.impostazioni);
    state.impostazioni.chiave = payload.chiave || { presente: false, origine: "", coda: "" };
    state.impostazioni.percorsoChiave = String(payload.percorsoChiave || "");
    state.impostazioni.valori = { ...(payload.impostazioni || {}) };
    state.impostazioni.predefinite = { ...(payload.predefinite || {}) };
    state.impostazioni.caricate = true;
    state.impostazioni.errore = "";
  } catch (error) {
    state.impostazioni.errore = `Impostazioni non lette: ${error.message}`;
  } finally {
    state.impostazioni.caricamento = false;
    render();
  }
}

async function loadModels() {
  if (state.impostazioni.modelli.caricamento) return;
  state.impostazioni.modelli.caricamento = true;
  state.impostazioni.modelli.errore = "";
  render();
  try {
    const payload = await requestJson(API.impostazioniModelli);
    state.impostazioni.modelli.elenco = asArray(payload.modelli).filter((voce) => voce && voce.id);
    state.impostazioni.modelli.avviso = String(payload.avviso || "");
    // `ok === false` here isn't a page failure: the list is a convenience,
    // and the identifier can always be typed by hand.
    state.impostazioni.modelli.errore = payload.ok === false ? String(payload.messaggio || "Elenco non disponibile.") : "";
    state.impostazioni.modelli.caricati = true;
  } catch (error) {
    state.impostazioni.modelli.elenco = [];
    state.impostazioni.modelli.errore = error.message;
  } finally {
    state.impostazioni.modelli.caricamento = false;
    render();
  }
}

async function saveApiKey() {
  const chiave = state.impostazioni.nuovaChiave;
  if (!chiave || state.impostazioni.salvandoChiave) return;
  state.impostazioni.salvandoChiave = true;
  state.impostazioni.errore = "";
  state.impostazioni.messaggio = "";
  render();
  try {
    const payload = await requestJson(API.impostazioniChiave, {
      method: "POST",
      body: JSON.stringify({ chiave }),
    });
    state.impostazioni.chiave = payload.chiave || state.impostazioni.chiave;
    state.impostazioni.messaggio = String(payload.messaggio || "Chiave salvata.");
    showToast("Chiave salvata.");
  } catch (error) {
    state.impostazioni.errore = `Chiave non salvata: ${error.message}`;
    showToast("Chiave non salvata.", "error");
  } finally {
    // Cleared on every branch: once submitted, the value has no reason to
    // stay on the page.
    state.impostazioni.nuovaChiave = "";
    state.impostazioni.salvandoChiave = false;
    render();
  }
}

async function testConnection() {
  if (state.impostazioni.provando) return;
  state.impostazioni.provando = true;
  state.impostazioni.prova = null;
  state.impostazioni.errore = "";
  state.impostazioni.messaggio = "";
  render();
  try {
    // The pasted key is not cleared here: it's tested before saving, and
    // someone who just saw "it works" needs to be able to press "Salva"
    // without pasting it again.
    state.impostazioni.prova = await requestJson(API.impostazioniProva, {
      method: "POST",
      body: JSON.stringify({
        chiave: state.impostazioni.nuovaChiave || undefined,
        model: modelloConfigurato() || undefined,
      }),
    });
  } catch (error) {
    state.impostazioni.errore = `Prova non riuscita: ${error.message}`;
  } finally {
    state.impostazioni.provando = false;
    render();
  }
}

// A number typed into a numeric field arrives as a string. The local
// service rejects "3" for a threshold that's 3.0, and that strictness is
// correct: this converts it before sending, and anything that isn't a
// number is sent as-is, since the service itself writes the message about
// the decimal separator.
function settingsNumber(valore) {
  const testo = String(valore ?? "").trim();
  if (!testo) return testo;
  const numero = Number(testo);
  return Number.isFinite(numero) ? numero : testo;
}

async function saveSettings() {
  if (state.impostazioni.salvando) return;
  state.impostazioni.salvando = true;
  state.impostazioni.errore = "";
  state.impostazioni.messaggio = "";
  render();
  try {
    const impostazioni = { model: modelloConfigurato() };
    for (const voce of LIMITI_AI) impostazioni[voce.nome] = settingsNumber(state.impostazioni.valori?.[voce.nome]);
    const payload = await requestJson(API.impostazioni, {
      method: "POST",
      body: JSON.stringify({ impostazioni }),
    });
    state.impostazioni.valori = { ...(payload.impostazioni || {}) };
    state.impostazioni.messaggio = String(payload.messaggio || "Impostazioni salvate.");
    showToast("Impostazioni salvate.");
  } catch (error) {
    // The local service's message is already in Italian and already
    // explained — it might say, for instance, that decimals use a dot — so
    // it arrives as-is.
    state.impostazioni.errore = `Impostazioni non salvate. ${error.message}`;
    showToast("Impostazioni non salvate.", "error");
  } finally {
    state.impostazioni.salvando = false;
    render();
  }
}

stepperElement.addEventListener("click", (event) => {
  // The fourth entry — "Impostazioni", shown only while open — carries no
  // `data-step` because it isn't a step of the workflow: pressing it closes
  // the page and returns to where you were.
  if (event.target.closest('[data-action="chiudi-impostazioni"]')) {
    closeSettings();
    return;
  }
  const button = event.target.closest("[data-step]");
  if (!button || !state.review || state.compiling) return;
  goToStep(button.dataset.step);
});

appElement.addEventListener("click", (event) => {
  // The dark backdrop closes the dialog, but only if the click actually
  // landed there. Deliberately not routed through `data-action`: `closest()`
  // would climb from the click target up to the backdrop, so a click inside
  // the dialog that lands on a margin — between two fields, next to a title
  // — would close it and discard whatever was being done. In the
  // order-column dialog that would mean losing the choice just made.
  if (event.target?.classList?.contains?.("dialog-backdrop") && chiudiLaFinestraInCima()) return;
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  // While compiling, suppliers can't be changed out from under the
  // documents being created.
  if (state.compiling && ["next", "previous", "move-supplier", "apply-supplier-move", "undo-supplier-move"].includes(action)) return;
  if (action === "retry") loadReview();
  if (action === "riprova-salvataggio") {
    state.tentativiDiSalvataggio = 0;
    saveState();
  }
  if (action === "nuova-comparazione") {
    state.nuovaComparazione.chiedendo = true;
    render();
    return;
  }
  if (action === "annulla-nuova-comparazione") {
    state.nuovaComparazione.chiedendo = false;
    render();
    return;
  }
  if (action === "conferma-nuova-comparazione") {
    cominciaNuovaComparazione();
    return;
  }
  if (action === "apri-impostazioni") openSettings();
  if (action === "chiudi-impostazioni") closeSettings();
  if (action === "salva-chiave") saveApiKey();
  if (action === "prova-connessione") testConnection();
  if (action === "salva-impostazioni") saveSettings();
  if (action === "ricarica-modelli") loadModels();
  if (action === "next") goToStep(state.currentStep + 1);
  if (action === "previous") goToStep(state.currentStep - 1);
  if (action === "upload") uploadFiles(target.dataset.uploadRole || "suppliers");
  if (action === "ask-delete-upload") {
    state.uploadDeletion.confirming = String(target.dataset.uploadName || "");
    render();
  }
  if (action === "cancel-delete-upload") {
    state.uploadDeletion.confirming = "";
    render();
  }
  if (action === "confirm-delete-upload") deleteUploadedList(String(target.dataset.uploadName || ""));
  if (action === "avvia-pipeline") avviaRicalcolo();
  // The "documents have changed" banner also shows on pages 2 and 3, where
  // the recompute button isn't visible: this command goes back to where it
  // is.
  if (action === "vai-al-ricalcolo") goToStep(1);
  if (action === "validate-schemas") validateSchemas();
  if (action === "confirm-schemas") confirmSchemas();
  if (action === "apri-colonne") apriColonneDocumento(String(target.dataset.uploadName || ""));
  if (action === "chiudi-colonne") chiudiColonneDocumento();
  if (action === "esci-dalle-colonne") chiudiColonneDocumento({ confermato: true });
  if (action === "resta-nelle-colonne") {
    state.schemaMapping.chiedendoUscita = false;
    render();
  }
  if (action === "prova-colonne") provaColonneDocumento();
  if (action === "salva-colonne") salvaColonneDocumento();
  if (action === "compile") compileOrders();
  if (action === "open-catalog") openCatalog();
  if (action === "close-catalog") chiudiLaFinestraInCima();
  if (action === "add-catalog-product") addCatalogProduct(String(target.dataset.catalogProductId || ""));
  if (action === "apri-listino") {
    apriIlListino(
      String(target.dataset.productId || ""),
      String(target.dataset.supplierId || ""),
      target.dataset.riga || null,
    );
  }
  if (action === "apri-colonna-ordine") apriLaColonnaDOrdine(String(target.dataset.supplierId || ""));
  if (action === "chiudi-colonna-ordine") chiudiLaFinestraInCima();
  if (action === "salva-colonna-ordine") salvaLaColonnaDOrdine();
  if (action === "close-listino") chiudiLaFinestraInCima();
  if (action === "abbina-riga") abbinaLaRiga(String(target.dataset.riga || ""));
  if (action === "listino-indietro") {
    state.listino.da = Math.max(0, state.listino.da - state.listino.quante);
    caricaIlListino();
  }
  if (action === "listino-avanti") {
    state.listino.da += state.listino.quante;
    caricaIlListino();
  }
  if (action === "togli-uguaglianza") {
    togliLUguaglianza(String(target.dataset.codici || "").split(",").filter(Boolean));
  }
  if (action === "conferma-abbinamento") {
    confermaLAbbinamento(
      String(target.dataset.productId || ""),
      target.dataset.confermato === "true",
    );
  }
  if (action === "rifiuta-abbinamento") {
    rifiutaLAbbinamento(
      String(target.dataset.productId || ""),
      String(target.dataset.supplierId || ""),
      target.dataset.rifiutata === "true",
    );
  }
  if (action === "answer-candidate") {
    answerRejectedCandidate(
      String(target.dataset.productId || ""),
      String(target.dataset.supplierId || ""),
      String(target.dataset.candidateKey || ""),
      target.dataset.accepted === "true",
    );
  }
  if (action === "exclude-product") {
    const product = findProduct(String(target.dataset.productId || ""));
    if (product) excludeProduct(product);
  }
  if (action === "restore-product") {
    const productId = String(target.dataset.productId || "");
    const product = findProduct(productId);
    // Restores the previous choices, not empty-handed: without them,
    // "Rimetti nell'ordine" would put the product back at zero quantity —
    // i.e. back in the list but still out of the order. If there are none
    // — cleared browser storage, for instance — it falls back to that, the
    // best that can be done without inventing a quantity.
    if (product) restoreProduct(product, state.sceltePrimaDellEsclusione.get(productId) || null);
  }
  if (action === "move-supplier") openSupplierMove(String(target.dataset.supplierId || ""));
  if (action === "close-supplier-move") chiudiLaFinestraInCima();
  if (action === "apply-supplier-move") applySupplierMove();
  if (action === "undo-supplier-move") undoSupplierMove();
  if (action === "undo-exclude" && state.exclusionUndo) {
    const previous = state.exclusionUndo;
    const product = findProduct(previous.id);
    if (product) restoreProduct(product, previous);
  }
  if (action === "change-quantity") {
    const product = findProduct(String(target.dataset.productId || ""));
    if (product) {
      product.quantity = Math.min(100000, Math.max(0, Math.trunc(orderQuantity(product) + finiteNumber(target.dataset.delta))));
      product.quantitySource = "utente";
      if (product.quantity === 0) product.confirmed = false;
      scadonoGliAnnulliDiQuantita();
      invalidateCompilationAcceptance();
      scheduleSave();
      rerenderPreservingFocus();
    }
  }
  if (action === "summary-remove-product") {
    const product = findProduct(String(target.dataset.productId || ""));
    if (product) {
      // Recorded before zeroing: the product disappears from this page the
      // same instant, since `orderedProducts()` keeps only items with
      // quantity above zero. Without this record, undoing it would mean
      // finding the product again among hundreds on page 2 and retyping a
      // forgotten quantity.
      state.rimozioneUndo = {
        id: product.id,
        name: product.name,
        quantity: orderQuantity(product),
        quantitySource: product.quantitySource,
        confirmed: product.confirmed,
      };
      product.quantity = 0;
      product.quantitySource = "utente";
      product.confirmed = false;
      invalidateCompilationAcceptance();
      scheduleSave();
      rerenderPreservingFocus();
    }
  }
  if (action === "undo-remove-product" && state.rimozioneUndo) {
    const precedente = state.rimozioneUndo;
    const product = findProduct(precedente.id);
    if (product) {
      // Also clears the exclusion. Between the "x" and the undo, the user
      // could have gone to page 2 and pressed "Escludi dall'ordine" on the
      // same product: restoring only the quantity would leave a product
      // marked "Escluso" with a nonzero carton count — counted in page 3's
      // subtotals and total, then zeroed out by the local service. The
      // button says "Rimetti nell'ordine", and this is what that means —
      // the same thing `restoreProduct` does.
      state.excludedProductIds.delete(precedente.id);
      persistExcludedProducts();
      product.quantity = precedente.quantity;
      product.quantitySource = precedente.quantitySource;
      // Also the confirmation, which the command clears together with the
      // quantity: without this, the product would return to the order with
      // compilation blocked and no line explaining why.
      product.confirmed = precedente.confirmed;
    }
    state.rimozioneUndo = null;
    invalidateCompilationAcceptance();
    scheduleSave();
    render();
  }
  if (action === "ask-delete-compilation") {
    state.compilazioni.confermaElimina = String(target.dataset.cartella || "");
    state.compilazioni.aperta = true;
    render();
  }
  if (action === "cancel-delete-compilation") {
    state.compilazioni.confermaElimina = "";
    render();
  }
  if (action === "confirm-delete-compilation") deleteCompilation(String(target.dataset.cartella || ""));
  if (action === "reset-suggested-quantities") {
    const azzerati = [];
    for (const product of state.review.products) {
      if (product.quantitySource === "gestionale" && orderQuantity(product) !== 0) {
        azzerati.push({ id: product.id, quantity: orderQuantity(product), confirmed: product.confirmed });
        product.quantity = 0;
        product.confirmed = false;
      }
    }
    if (azzerati.length) {
      state.azzeramentoUndo = { prodotti: azzerati };
      invalidateCompilationAcceptance();
      scheduleSave();
    } else {
      showToast("Nessuna quantità dal gestionale da azzerare.");
    }
    render();
  }
  if (action === "undo-azzeramento" && state.azzeramentoUndo) {
    for (const voce of state.azzeramentoUndo.prodotti) {
      const product = findProduct(voce.id);
      if (!product) continue;
      product.quantity = voce.quantity;
      // Restored as "gestionale", not "utente": as "utente" the button
      // wouldn't find these quantities again, and pressing it a second time
      // would do nothing.
      product.quantitySource = "gestionale";
      product.confirmed = voce.confirmed;
    }
    state.azzeramentoUndo = null;
    invalidateCompilationAcceptance();
    scheduleSave();
    render();
  }
  if (action === "summary-group") {
    state.summary.grouping = target.dataset.summaryGroup === "product" ? "product" : "supplier";
    scheduleSave();
    render();
  }
  if (action === "products-previous-page" || action === "products-next-page") {
    state.filters.page = Math.max(1, state.filters.page + (action === "products-next-page" ? 1 : -1));
    render();
    document.querySelector(".toolbar")?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  // The command written on the notice: applies the filter it names and goes
  // to where it's answered. The filter is checked against `FILTRI_PRODOTTO`
  // before it's written to state: a bad value would leave the page on an
  // empty list with no explanation.
  if (action === "vai-al-filtro") {
    const filtro = String(target.dataset.filtro || "");
    if (FILTRI_PRODOTTO[filtro]) {
      state.filters.status = filtro;
      // The search box and the type filter would narrow the list the
      // notice just promised, so they're cleared, the same way "Mostra i
      // prodotti" does for offers.
      state.filters.search = "";
      state.filters.type = "all";
      state.filters.promotionId = null;
      state.filters.page = 1;
      goToStep(2);
      document.querySelector(".product-list")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  if (action === "filter-promotion") {
    const promotionId = String(target.dataset.promotionId || "");
    if (promotionId && findPromotion(promotionId)) {
      state.filters.promotionId = promotionId;
      state.filters.search = "";
      state.filters.type = "all";
      state.filters.status = "all";
      state.filters.page = 1;
      render();
      document.querySelector(".product-list")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }
  if (action === "history-received" || action === "history-not-received") {
    answerPendingOrder(String(target.dataset.orderId || ""), action === "history-received");
  }
  if (action === "history-never") {
    answerPendingOrder(String(target.dataset.orderId || ""), false, { closed: true });
  }
  if (action === "clear-promotion-filter") {
    state.filters.promotionId = null;
    state.filters.page = 1;
    render();
  }
  if (action === "clear-promotion-query") {
    state.filters.promotionQuery = "";
    render();
  }
  if (action === "remove-file") {
    state.pendingFiles.splice(finiteNumber(target.dataset.fileIndex, -1), 1);
    render();
  }
  if (action === "togli-scartato") {
    state.fileScartati.splice(finiteNumber(target.dataset.scartatoIndice, -1), 1);
    render();
  }
});

appElement.addEventListener("change", (event) => {
  const target = event.target;

  if (target.dataset.schemaDocument) {
    const identificativo = String(target.dataset.schemaDocument || "");
    const valore = state.schemaMapping.values[identificativo];
    if (!valore) return;
    if (target.dataset.schemaColumn !== undefined) {
      const campo = String(target.dataset.schemaColumn || "");
      valore.columns = { ...(valore.columns || {}) };
      if (target.value) valore.columns[campo] = Number(target.value);
      else delete valore.columns[campo];
    } else {
      const campo = String(target.dataset.schemaField || "");
      valore[campo] = ["headerRow", "dataStartRow", "orderColumn"].includes(campo)
        ? (target.value ? Number(target.value) : "")
        : target.value;
      if (campo === "sheet") {
        const documento = (state.schemaMapping.data?.documents || []).find((voce) => String(voce.profileId || "") === identificativo);
        const foglio = schemaSelectedSheet(documento || {}, valore);
        valore.headerRow = Number(foglio.headerRows?.[0] || 1);
        valore.dataStartRow = valore.headerRow + 1;
        valore.columns = {};
        valore.orderColumn = "";
        // Row-break rules belong to that specific sheet: keeping one chosen
        // here would describe a row that doesn't exist in this sheet.
        valore.dataStartBreak = "";
        valore.markerText = "";
      }
      if (campo === "dataStartBreak") {
        const documento = (state.schemaMapping.data?.documents || []).find((voce) => String(voce.profileId || "") === identificativo);
        schemaApplicaSeparatore(valore, schemaSelectedSheet(documento || {}, valore));
      }
      if (campo === "headerRow" && Number(valore.dataStartRow) <= Number(valore.headerRow)) {
        valore.dataStartRow = Number(valore.headerRow) + 1;
        // The header row just decided where the data starts: a rule naming
        // a different row doesn't describe this cut anymore.
        valore.dataStartBreak = "";
        valore.markerText = "";
      }
    }
    delete state.schemaMapping.results[identificativo];
    state.schemaMapping.error = "";
    // `render()` REPLACES the HTML: the dropdown just used dies and a new
    // one is born, with no focus. Someone assigning twelve columns via
    // keyboard would restart from the top of the page twelve times. The id
    // already exists — `idCampoSchema` builds it from the two values
    // already in the markup — so it's the same before and after the
    // redraw: it just needs to be found again.
    const tornaSu = String(target.id || "");
    render();
    if (tornaSu) document.querySelector(`#${CSS.escape(tornaSu)}`)?.focus();
    return;
  }

  if (target.dataset.elencoModelli !== undefined) {
    // The dropdown fills the text field, which stays the real value: a
    // model released yesterday isn't in the list yet and still needs to be
    // typeable.
    if (target.value) {
      state.impostazioni.valori.model = String(target.value);
      render();
    }
    return;
  }

  if (target.dataset.impostazione) {
    state.impostazioni.valori[target.dataset.impostazione] = target.value;
    return;
  }

  if (target.matches("[data-picker-role]")) {
    addPendingFiles(target.files, target.dataset.pickerRole || "suppliers");
    return;
  }

  if (target.dataset.productQuantity) {
    const product = findProduct(target.dataset.productQuantity);
    if (!product) return;
    const value = finiteNumber(target.value, 0);
    product.quantity = Math.min(100000, Math.max(0, Math.trunc(value)));
    product.quantitySource = "utente";
    target.value = product.quantity;
    if (product.quantity === 0) product.confirmed = false;
    scadonoGliAnnulliDiQuantita();
    invalidateCompilationAcceptance();
    scheduleSave();
    render();
    return;
  }

  if (target.dataset.productSupplier) {
    const product = findProduct(target.dataset.productSupplier);
    if (!product) return;
    const previousOffer = selectedOffer(product);
    product.selectedSupplierId = String(target.value || "");
    product.confirmed = false;
    const newOffer = selectedOffer(product);
    if (previousOffer && newOffer && previousOffer.quantityFactor !== newOffer.quantityFactor) {
      flagSupplierFactorChange(product, previousOffer, newOffer);
    }
    invalidateCompilationAcceptance();
    scheduleSave();
    render();
    return;
  }

  if (target.dataset.offerChoice) {
    const product = findProduct(target.dataset.offerChoice);
    if (!product) return;
    const previousOffer = selectedOffer(product);
    product.selectedSupplierId = String(target.value || "");
    product.confirmed = false;
    const newOffer = selectedOffer(product);
    if (previousOffer && newOffer && previousOffer.quantityFactor !== newOffer.quantityFactor) {
      flagSupplierFactorChange(product, previousOffer, newOffer);
    }
    invalidateCompilationAcceptance();
    scheduleSave();
    rerenderPreservingFocus();
    return;
  }

  if (target.dataset.moveOption) {
    // Only changes the highlighted destination: no product moves until
    // "Sposta i prodotti" is pressed.
    state.supplierMove.choiceId = String(target.value || "");
    rerenderPreservingFocus();
    return;
  }

  if (target.matches("[data-below-threshold-confirm]")) {
    state.acceptBelowThreshold = Boolean(target.checked);
    state.compileResult = null;
    scheduleSave();
    rerenderPreservingFocus();
    return;
  }

  if (target.dataset.supplierDiscount !== undefined) {
    applicaScontoFornitore(target.dataset.supplierDiscount, target.value);
    return;
  }

  if (target.dataset.colonnaOrdine !== undefined) {
    state.colonnaOrdine = { ...state.colonnaOrdine, scelta: String(target.value || ""), errore: "" };
    render();
    return;
  }

  if (target.dataset.listinoFornitore !== undefined) {
    state.listino.fornitore = String(target.value || "");
    state.listino.nome = "";
    state.listino.da = 0;
    state.listino.rigaFuoco = null;
    // The previous supplier's rows are cleared immediately: leaving them in
    // the table, clickable, under the new supplier's name for the length of
    // the request would let "È questo" send that row number to the wrong
    // supplier — a match on the wrong price list, i.e. a wrong order. Same
    // reason for the outcome message: "Abbinato" shown over another
    // supplier's list would read as something that didn't happen.
    state.listino.righe = [];
    state.listino.esito = "";
    state.listino.totale = 0;
    state.listino.trovate = 0;
    state.listino.scartate = 0;
    caricaIlListino();
    return;
  }

  if (target.dataset.filter && target.dataset.filter !== "search") {
    state.filters[target.dataset.filter] = target.value;
    state.filters.page = 1;
    // Touching a filter by hand takes back control from the "Mostra i
    // prodotti" action in the offers panel. Sorting doesn't: it isn't a
    // filter, and changing the order shouldn't make the list being looked
    // at disappear.
    if (target.dataset.filter !== "sort") state.filters.promotionId = null;
    render();
    return;
  }

  if (target.dataset.summaryFilter) {
    state.summary[target.dataset.summaryFilter] = target.value;
    render();
  }
});

appElement.addEventListener("input", (event) => {
  const target = event.target;
  if (target.dataset.schemaDocument && target.dataset.schemaField) {
    const identificativo = String(target.dataset.schemaDocument || "");
    const valore = state.schemaMapping.values[identificativo];
    if (!valore) return;
    const campo = String(target.dataset.schemaField || "");
    valore[campo] = ["headerRow", "dataStartRow", "orderColumn"].includes(campo)
      ? (target.value ? Number(target.value) : "")
      : target.value;
    delete state.schemaMapping.results[identificativo];
    state.schemaMapping.error = "";
    return;
  }
  if (target.dataset.chiaveOpenrouter !== undefined) {
    // The key stays only in memory: nothing re-renders here, so the value
    // never passes through the page's HTML. Only the button that depends
    // on it is updated by hand.
    state.impostazioni.nuovaChiave = target.value;
    const salva = document.querySelector('[data-action="salva-chiave"]');
    if (salva) salva.disabled = state.impostazioni.salvandoChiave || state.impostazioni.provando || !state.impostazioni.nuovaChiave;
    return;
  }
  if (target.dataset.impostazione) {
    state.impostazioni.valori[target.dataset.impostazione] = target.value;
    // Only the model field has something to redraw while typing: the price
    // and the "not in the list" notice. The limits don't, and redrawing
    // them would jump the cursor on every keystroke.
    if (target.dataset.impostazione === "model") rerenderPreservingFocus();
    return;
  }
  if (target.dataset.productQuantity) {
    const product = findProduct(target.dataset.productQuantity);
    if (!product) return;
    const value = finiteNumber(target.value, 0);
    product.quantity = Math.min(100000, Math.max(0, Math.trunc(value)));
    product.quantitySource = "utente";
    if (product.quantity === 0) product.confirmed = false;
    scadonoGliAnnulliDiQuantita();
    // The render is delayed by 180 ms, but issues are invalidated right
    // away: otherwise a read within that window would use the calculation
    // from before the keystroke.
    invalidateIssues();
    invalidateCompilationAcceptance();
    scheduleSave();
    window.clearTimeout(state.quantityRenderTimer);
    state.quantityRenderTimer = window.setTimeout(() => rerenderPreservingFocus(), 180);
    return;
  }
  if (target.matches("[data-promotion-search]")) {
    // The offers panel stays open while typing: it redraws while keeping
    // focus, same as the product search.
    state.filters.promotionQuery = target.value;
    rerenderPreservingFocus();
    return;
  }
  if (target.matches("[data-catalog-search]")) {
    state.catalog.query = target.value;
    scheduleCatalogSearch();
    return;
  }
  if (target.matches("[data-uguaglianze-cerca]")) {
    cercaFraLeUguaglianze(target.value);
    rerenderPreservingFocus();
    return;
  }
  if (target.matches("[data-listino-cerca]")) {
    // The price list runs to eight thousand rows and lives on the local
    // service: this waits for typing to stop instead of asking on every
    // keystroke.
    state.listino.query = target.value;
    state.listino.da = 0;
    if (state.listino.timer) clearTimeout(state.listino.timer);
    state.listino.timer = window.setTimeout(() => caricaIlListino(), 250);
    return;
  }
  if (target.dataset.filter !== "search") return;
  state.filters.search = target.value;
  state.filters.page = 1;
  state.filters.promotionId = null;
  rerenderPreservingFocus();
});

// An order quantity shouldn't change without an explicit gesture. Browsers
// that still do this (Firefox, Chromium before version 119) let the wheel
// over a focused number field increment it one notch per tick while the
// page stays put. This blurs the field and cancels the event, so the next
// scroll scrolls the page normally.
appElement.addEventListener("wheel", (event) => {
  const field = event.target.closest?.('input[type="number"]');
  if (!field || document.activeElement !== field) return;
  event.preventDefault();
  field.blur();
}, { passive: false });

// The <details> "toggle" event doesn't bubble, but the capture phase still
// sees it: kept to remember whether the offers panel was left open.
appElement.addEventListener("toggle", (event) => {
  const target = event.target;
  if (target instanceof HTMLDetailsElement && target.dataset.promotionCatalog !== undefined) {
    state.promotionCatalogOpen = target.open;
  }
  if (target instanceof HTMLDetailsElement && target.dataset.compilazioni !== undefined) {
    state.compilazioni.aperta = target.open;
  }
  ricordaApertura(target);
}, true);

appElement.addEventListener("dragover", (event) => {
  const dropzone = event.target.closest("[data-dropzone]");
  if (!dropzone) return;
  event.preventDefault();
  dropzone.classList.add("is-dragging");
});

appElement.addEventListener("dragleave", (event) => {
  const dropzone = event.target.closest("[data-dropzone]");
  if (!dropzone || dropzone.contains(event.relatedTarget)) return;
  dropzone.classList.remove("is-dragging");
});

appElement.addEventListener("drop", (event) => {
  const dropzone = event.target.closest("[data-dropzone]");
  if (!dropzone) return;
  event.preventDefault();
  dropzone.classList.remove("is-dragging");
  addPendingFiles(event.dataTransfer.files, dropzone.dataset.uploadRole || "suppliers");
});

window.addEventListener("keydown", (event) => {
  // Esc closes whichever of the four dialogs is on top. This matters for
  // the price-list viewer in particular: it's the way out when automatic
  // matching couldn't find a row, i.e. it opens exactly when something has
  // already gone wrong.
  if (event.key === "Escape") chiudiLaFinestraInCima();
});

window.addEventListener("beforeunload", (event) => {
  if (mode === "live" && state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

loadReview();
// Reloading the page while the pipeline is running should find the bar
// where it was: the recompute runs in the local service and doesn't stop
// because the browser closed. An error here means nothing — just that
// there's no recompute to resume.
//
// This also recovers the outcome, not just the bar. A recompute that
// stopped partway, or finished with warnings, needs to survive a page
// reload, since those warnings are exactly what says the compilation needs
// another look. The service keeps the final state (in
// `pipeline_status.json`, reread even after a restart).
if (mode === "live") {
  requestJson(API.pipelineStato)
    .then((stato) => {
      const esito = String(stato?.stato || "");
      // IN_ATTESA doesn't always mean nothing has ever run: with
      // `cambiamento`, it means the documents changed since the last
      // comparison, and that banner needs to survive a page reload too —
      // reopening the program the following Monday is exactly when knowing
      // the on-screen prices are stale matters most. Without
      // `cambiamento`, the simpler reading still holds: no recompute to
      // resume, nothing to show.
      if (!esito || (esito === "IN_ATTESA" && !cambiamentoDocumenti(stato))) return;
      state.pipeline.stato = stato;
      state.pipeline.chiesto = true;
      render();
      if (schemaMappingRequired(stato)) loadSchemaMapping();
      // Keeps polling only a live run: on a finished one, the timer would
      // repeat the same response forever.
      if (esito === "IN_CORSO") pianificaControlloPipeline();
    })
    .catch(() => {});
}

// ---------------------------------------------------------------------------
// The published-version date, top right.
//
// This PC syncs itself from GitHub on every launch, and when it can't —
// expired credentials, no network — it deliberately doesn't block anything:
// it logs a line to the console and starts with what it has. Nobody reads
// that line, and a program that's been stuck for months looks identical to
// one updated minutes ago. With the date shown here, a glance — or having
// someone read it out over a phone call — is enough to tell which one it is.
//
// Lives in the topbar, outside `#app`: `render()` replaces only `#app`'s
// HTML, so this line is written once and no redraw carries it away.
if (mode === "live") {
  const elementoVersione = document.querySelector("#versione-pubblicata");
  if (elementoVersione) {
    requestJson(API.salute)
      .then((salute) => {
        const quando = formatDayMonthYear(salute?.versionePubblicata);
        if (quando) {
          const frase = `versione del ${quando}`;
          elementoVersione.textContent = frase;
          // Precise date and time for anyone debugging an issue: the page
          // only needs the day, but pinpointing a specific run needs the
          // exact moment.
          elementoVersione.title = formatDateTime(salute.versionePubblicata);
          // Without this, a screen reader would announce the tooltip
          // instead: `title` alone becomes the element's accessible name
          // and overrides the visible text.
          elementoVersione.setAttribute("aria-label", frase);
        } else {
          // No date means git isn't available here or isn't responding:
          // that's an answer, not a failure, and it's stated instead of
          // leaving a blank.
          elementoVersione.textContent = "versione sconosciuta";
          elementoVersione.title = "Non riesco a leggere quale versione sta girando su questo computer.";
        }
      })
      .catch(() => {});
  }
}
