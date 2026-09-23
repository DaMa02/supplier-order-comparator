/*
 * Contratto REST atteso
 *
 * GET  /api/review
 *   -> { run, files, suppliers, products, warnings }
 *
 * POST /api/upload
 *   <- { files: [{ name, data }] } dove data è base64 senza prefisso data:
 *   -> { files, status, message }
 *
 * PUT  /api/state
 *   <- { runId, currentStep, summaryGrouping, products: [{ id, quantity, selectedSupplierId, confirmed, excluded }] }
 *
 * GET  /api/products/search e POST /api/products/add
 *   -> ricerca e aggiunta di prodotti dai cataloghi correnti
 *
 * GET  /api/history/pending
 *   -> { ok, pending: [{ orderId, supplier, supplierName, createdAt,
 *                        answeredAt, askAgainAt, lineCount, totalNet }] }
 *   Solo gli ordini già compilati e non ancora confermati come ricevuti.
 *
 * POST /api/history/answer
 *   <- { orderId, received } oppure { orderId, closed: true }
 *      SÌ = ricevuto; NO = chiedi di nuovo fra sette giorni;
 *      closed = non arriverà più. Sempre per l'intero ordine.
 *   -> { ok, pending: [...] }  elenco aggiornato
 *
 * POST /api/matches/answer
 *   <- { runId, productId, supplierId, candidateKey, accepted }
 *   Conferma o esclude una riga proposta per il solo confronto corrente.
 *
 * POST /api/uploads/elimina
 *   <- { name }  elimina una singola copia di un listino caricato
 *
 * POST /api/ordini/elimina
 *   <- { cartella }  elimina una compilazione e i relativi promemoria
 *
 * POST /api/suppliers/move-preview
 *   <- stesso snapshot di /api/state più { from: "<idFornitore di partenza>" }
 *   -> { ok, from, fromName, movableCount, currentNetTotal, options: [{
 *          id, kind, label, movedCount, movableCount, deltaNet,
 *          assignments: [{ productId, productName, toSupplierId, previousFactor,
 *                          newFactor, factorChanged, needsConfirmation,
 *                          previousLineNet, newLineNet }],
 *          leftBehind: [{ productId, productName, reason }],
 *          supplierTotalsAfter: [{ supplierId, supplierName, netTotalBefore,
 *                                  netTotalAfter, threshold,
 *                                  meetsThresholdBefore, meetsThresholdAfter }] }] }
 *   È solo un preventivo: non scrive nulla e non cambia l'ordine. Lo spostamento
 *   vero passa dal PUT /api/state come qualsiasi altra modifica.
 *
 * POST /api/compile
 *   <- stesso snapshot di /api/state
 *   -> { ok, message, cartella, zipUrl, zipNome, outputs: [{ name, url, tipo }] }
 *   zipUrl è null quando la compilazione non ha prodotto nessun listino.
 *
 * GET  /api/ordini
 *   -> { ok, compilazioni: [{ cartella, etichetta, creatoIl, stato,
 *          fornitori: [{ id, nome, totaleNetto }], totaleNetto, righe, listini,
 *          file: [{ nome, tipo, url }], zipUrl, zipNome, completa }] }
 *   Dalla più recente. `completa: false` vuol dire che l'audit di quella
 *   compilazione non si legge: la cartella c'è lo stesso e i suoi file si
 *   scaricano lo stesso.
 *
 * I dati dimostrativi vengono usati esclusivamente con ?demo=1.
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
  // Il ricalcolo completo del confronto: un pulsante, e la catena va da sola.
  pipelineAvvia: configuredApi.pipelineAvvia || "/api/pipeline/avvia",
  pipelineStato: configuredApi.pipelineStato || "/api/pipeline/stato",
  schemasPending: configuredApi.schemasPending || "/api/schemas/pending",
  schemasValidate: configuredApi.schemasValidate || "/api/schemas/validate",
  schemasConfirm: configuredApi.schemasConfirm || "/api/schemas/confirm",
  // Le colonne di un listino già caricato, riviste quando si vuole: senza
  // queste tre la mappatura si poteva vedere solo quando la catena si fermava.
  colonneDocumento: configuredApi.colonneDocumento || "/api/schemas/documento",
  colonneProva: configuredApi.colonneProva || "/api/schemas/documento/prova",
  colonneSalva: configuredApi.colonneSalva || "/api/schemas/documento/salva",
  // Quali colonne il confronto vivo ha letto in ogni documento. Non è la
  // mappatura guidata (che compare solo quando il programma NON riconosce un
  // file): è quello che ha fatto sui file che ha riconosciuto.
  schemasColumns: configuredApi.schemasColumns || "/api/schemas/columns",
  historyPending: configuredApi.historyPending || "/api/history/pending",
  historyAnswer: configuredApi.historyAnswer || "/api/history/answer",
  matchAnswer: configuredApi.matchAnswer || "/api/matches/answer",
  // Il listino di un fornitore come il programma l'ha letto, e l'abbinamento
  // fatto a mano su una sua riga.
  listino: configuredApi.listino || "/api/listino",
  matchAbbina: configuredApi.matchAbbina || "/api/matches/abbina",
  // «Non è lo stesso articolo»: il no all'offerta di un fornitore su un
  // prodotto, e il ritorno indietro. Rotta a sé come il sì alla proposta
  // automatica, e per la stessa ragione: è una risposta, non una quantità, e va
  // registrata subito in una memoria che sopravvive al ricalcolo.
  matchRifiuta: configuredApi.matchRifiuta || "/api/matches/rifiuta",
  // Le dichiarazioni «questi due codici sono lo stesso articolo». Rotta a sé e
  // non dentro il confronto: si rileggono in Impostazioni, che si apre anche
  // quando un confronto non c'è.
  uguaglianze: configuredApi.uguaglianze || "/api/matches/uguaglianze",
  uguaglianzaTogli: configuredApi.uguaglianzaTogli || "/api/matches/uguaglianze/togli",
  // Tutto quello che hai confermato, come file da salvare. `conferme.db` è la
  // memoria «per sempre» del programma e non ha nessuna copia di sicurezza:
  // questa è l'unica via per fargliene una senza copiare a mano un file
  // SQLite aperto.
  confermeEsporta: configuredApi.confermeEsporta || "/api/conferme/esporta",
  // La colonna in cui il programma scrive le quantità ordinate. Si legge e si
  // cambia dallo stesso indirizzo: la GET dice fra quali colonne scegliere, la
  // POST sposta — e la prova che quella colonna sia scrivibile è la stessa che
  // attiva la compilazione, quindi il «no» che torna qui è quello vero.
  colonnaOrdine: configuredApi.colonnaOrdine || "/api/schemas/order-column",
  uploadDelete: configuredApi.uploadDelete || "/api/uploads/elimina",
  comparazioneNuova: configuredApi.comparazioneNuova || "/api/comparazione/nuova",
  supplierMovePreview: configuredApi.supplierMovePreview || "/api/suppliers/move-preview",
  // Lo sconto di testata su tutto il listino di un fornitore.
  supplierDiscount: configuredApi.supplierDiscount || "/api/suppliers/discount",
  // Le compilazioni già fatte, una cartella datata per ciascuna.
  ordini: configuredApi.ordini || "/api/ordini",
  ordiniDelete: configuredApi.ordiniDelete || "/api/ordini/elimina",
  // Impostazioni della fase AI. La chiave viaggia soltanto nel corpo di una
  // POST: il servizio locale stampa la riga di richiesta a ogni chiamata, e una
  // chiave passata come parametro dell'indirizzo finirebbe stampata.
  impostazioni: configuredApi.impostazioni || "/api/impostazioni",
  impostazioniModelli: configuredApi.impostazioniModelli || "/api/impostazioni/modelli",
  impostazioniChiave: configuredApi.impostazioniChiave || "/api/impostazioni/chiave",
  impostazioniProva: configuredApi.impostazioniProva || "/api/impostazioni/prova",
};

const mode = new URLSearchParams(window.location.search).get("demo") === "1" ? "demo" : "live";

// Finestra "Sposta tutto su un altro fornitore" chiusa: from vuoto significa che
// non c'è nessuna richiesta in corso e nessuna destinazione da scegliere.
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
    // Impostato da "Mostra i prodotti" nel pannello offerte (pagina 2): filtra
    // l'elenco sui soli prodotti coinvolti da quella promozione. Si azzera da
    // solo appena l'utente tocca di nuovo ricerca/tipo/stato, oppure col
    // pulsante "Rimuovi filtro" del chip dedicato.
    promotionId: null,
    // Ricerca nel testo delle condizioni dei listini: con centinaia di voci
    // scorrere l'elenco non è un modo di trovare niente.
    promotionQuery: "",
    // In che ordine si scorre l'elenco della pagina 2. "gestionale" è l'ordine
    // in cui il documento del gestionale elenca i prodotti — resta il
    // predefinito, perché è l'ordine in cui chi ordina li ha scritti — e le
    // altre due mettono in fila i nomi, che con cinquecento righe è l'unico
    // modo di ritrovare un prodotto senza cercarlo per nome.
    sort: "gestionale",
  },
  summary: {
    grouping: "supplier",
    supplierId: "all",
    sort: "name",
  },
  // Il riquadro delle offerte contiene il campo di ricerca: se non si ricorda
  // che era aperto, si richiude a ogni ridisegno e il campo sparisce da sotto
  // le dita mentre si scrive.
  promotionCatalogOpen: false,
  pendingFiles: [],
  // I documenti trascinati e non riconosciuti. Stanno FUORI da `pendingFiles`,
  // che è l'array da cui parte la richiesta al servizio locale: qui dentro non
  // deve finire niente che possa essere spedito per sbaglio. Restano finché non
  // li si toglie, perché un messaggio che vive 3,6 secondi non è un posto dove
  // un'informazione esiste.
  fileScartati: [],
  // Quanti salvataggi di fila non sono riusciti: dopo tre il programma smette
  // di riprovare da solo e lascia il comando in cima alla pagina.
  tentativiDiSalvataggio: 0,
  excludedProductIds: new Set(),
  exclusionUndo: null,
  // productId -> { quantity, selectedSupplierId, confirmed }: che cosa aveva il
  // prodotto prima di essere escluso.
  // ⚠ Esiste perché `exclusionUndo` è UNO SOLO e scade: la fascia «… è stato
  // escluso. [Annulla]» rimetteva quantità e fornitore, ma «Rimetti
  // nell'ordine» — l'unico comando che resta dopo la fascia, e l'unico dopo un
  // ricaricamento — rimetteva il prodotto nell'elenco **con la quantità a
  // zero**. Il prodotto tornava, sembrava a posto e non veniva ordinato, e la
  // quantità di prima non era scritta da nessuna parte. Vive accanto agli
  // esclusi nella memoria del browser, sotto la chiave della run: al ricalcolo
  // se ne va con loro, che è giusto — quelle scelte parlavano del confronto di
  // prima.
  sceltePrimaDellEsclusione: new Map(),
  // Il gemello di `exclusionUndo` per il «×» del riepilogo (pagina 3). Sono due
  // comandi diversi — quello esclude il prodotto dall'elenco, questo ne azzera
  // la quantità — e tutti e due fanno sparire il prodotto da sotto gli occhi
  // nello stesso istante in cui si preme. Tiene quantità E conferma: il comando
  // azzera tutte e due, e rimettere solo la quantità farebbe tornare il prodotto
  // senza la sua conferma, cioè con la compilazione bloccata.
  rimozioneUndo: null,
  // Le quantità azzerate in blocco da «Azzera le quantità proposte dal
  // gestionale», per rimetterle. È il comando di massa della pagina 2: senza
  // annullo era l'azione più grave della schermata con la rete di sicurezza più
  // sottile — escludere UN prodotto, che è meno grave, ce l'aveva già.
  azzeramentoUndo: null,
  // productId -> { supplierName, quantityText, previousFactor, newFactor, previousPieces, newPieces }
  // Evidenzia temporaneamente una scheda quando il cambio di fornitore cambia i pezzi per collo.
  supplierChangeNotices: new Map(),
  supplierChangeTimers: new Map(),
  // Spostamento di tutti i prodotti di un fornitore su un altro (pagina 3).
  // I prezzi e le differenze arrivano dal servizio locale: qui si tiene solo
  // quale fornitore si sta svuotando e quale destinazione l'utente ha scelto.
  supplierMove: emptySupplierMove(),
  // Assegnazioni e conferme di prima dell'ultimo spostamento: servono al comando
  // "Rimetti i fornitori di prima", come state.exclusionUndo per i prodotti
  // esclusi.
  supplierMoveUndo: null,
  // «Inizia nuova comparazione»: `chiedendo` è la riga di conferma aperta,
  // `inCorso` il tempo fra la conferma e la risposta del servizio. Non serve
  // ricordare altro: quello che il comando fa lo si rilegge dalla pagina
  // rifatta, non da una bandierina.
  nuovaComparazione: { chiedendo: false, inCorso: false },
  // Ordini già compilati la settimana scorsa e non ancora confermati come ricevuti.
  // È un aiuto per non riordinare merce in arrivo: se il servizio locale non
  // risponde l'elenco resta vuoto e la pagina funziona esattamente come prima.
  history: {
    pending: [],
    loading: false,
    // Perché l'elenco degli ordini in sospeso non c'è. Vuoto = non c'è niente
    // in sospeso; pieno = non si è potuto sapere, ed è tutt'altra notizia.
    errore: "",
    // orderId della domanda a cui si sta rispondendo in questo momento.
    answering: "",
    // orderId a cui l'utente ha già risposto in questa sessione: la domanda non
    // viene riproposta finché la pagina resta aperta.
    answered: new Set(),
  },
  // Le compilazioni precedenti, lette da GET /api/ordini scandendo le cartelle
  // datate. Non sono un requisito del passo 3: se l'elenco non arriva, il
  // riquadro lo dice e il riepilogo resta usabile com'era. Il browser non
  // ricalcola niente di quello che c'è qui dentro, lo mostra e basta.
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
  // Il visualizzatore dei listini: si apre da un prodotto e mostra il listino di
  // un fornitore come il programma l'ha letto. `prodottoId` è il prodotto da cui
  // si è partiti — è lui che si abbina — e `rigaFuoco` la riga già abbinata,
  // quella su cui la finestra si apre.
  listino: {
    aperto: false,
    prodottoId: "",
    fornitore: "",
    query: "",
    // Il numero dell'ultima lettura chiesta: una risposta che non porta questo
    // numero e' vecchia e non si applica.
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
    // Il listino chiesto non c'è, e il servizio dice perché. Non è `errore`:
    // la finestra funziona, è quel fornitore che non si legge.
    problema: "",
    esito: "",
    abbinando: "",
    timer: null,
  },
  // Il ricalcolo del confronto. `stato` è esattamente quello che risponde
  // GET /api/pipeline/stato: qui non si ricostruisce niente, si mostra.
  // `chiesto` distingue "ho premuto io il pulsante adesso" da "il servizio
  // ricorda una esecuzione di prima", che non deve far comparire la barra.
  // `controlliPersi` conta le richieste di stato andate a vuoto di fila;
  // `contattoPerso` è la frase che compare quando sono troppe, perché una barra
  // ferma senza una parola si legge come «sta ancora lavorando».
  // `avviando`: la richiesta di partenza è in volo. Sta fuori da `stato`, che è
  // quello che manda il servizio, perché è una cosa che sa solo questa scheda.
  pipeline: { stato: null, chiesto: false, errore: "", timer: null, controlliPersi: 0, contattoPerso: "", avviando: false },
  // Come sta ogni sezione apribile, per chiave: la pagina si ridisegna da sola
  // e senza questa mappa si richiudono sotto le dita. Il valore è `true` o
  // `false` — non basta «c'è la chiave», perché i riquadri che nascono aperti
  // hanno bisogno che il «chiuso» sia scritto, non dedotto da un'assenza.
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
    // Il documento di cui si stanno rivedendo le colonne **a mano**, aperto
    // dalla sua scheda nella pagina 1. Vuoto quando il selettore compare
    // perché la catena si è fermata: quello è un altro percorso, e riparte da
    // solo dopo la conferma.
    documento: "",
  },
  // Le colonne usate dal confronto vivo, per nome di documento. Si caricano una
  // volta per ricalcolo: cambiano solo quando cambia la run.
  colonneDocumenti: { caricate: false, caricando: false, runId: "", perNome: {}, motivo: "", errore: "" },
  // La finestra «in quale colonna si scrive l'ordine». Sta qui e non dentro
  // `schemaMapping` perché quella è la mappatura guidata, che si apre solo
  // quando il programma NON riconosce un documento: questa si apre sempre.
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
  // La `data-focus-key` del comando che ha aperto la finestra modale in cima:
  // alla chiusura il fuoco ci torna sopra. La stringa vuota vuol dire «quel
  // comando non aveva una chiave, ripiega su #workspace»; `null` vuol dire
  // «nessuna finestra da chiudere», e serve perche' le funzioni di chiusura
  // vengono chiamate anche quando non c'e' niente di aperto.
  fuocoPrimaDellaFinestra: null,
  loading: true,
  uploading: false,
  compiling: false,
  saving: false,
  dirty: false,
  saveVersion: 0,
  savedVersion: 0,
  // La versione dello stato SUL DISCO da cui questa scheda è partita. Non ha
  // niente a che vedere con saveVersion/savedVersion, che contano le modifiche
  // di questa scheda: serve al servizio locale per accorgersi che nel frattempo
  // ha salvato un'altra scheda, invece di lasciarle cancellare il lavoro a
  // vicenda in silenzio.
  stateVersion: 0,
  savePromise: null,
  runtimeError: "",
  uploadMessage: "",
  compileResult: null,
  // La compilazione andata male: frase in italiano e testo tecnico separati, così
  // il primo si legge e il secondo resta a disposizione senza occupare il posto.
  compileFailure: null,
  acceptBelowThreshold: false,
  saveTimer: null,
  quantityRenderTimer: null,
  // Pagina Impostazioni. Non è un passo del flusso: `currentStep` vale 1..3 sia
  // qui sia sul servizio locale, e infilarci un quarto valore lo farebbe
  // tagliare al salvataggio. È una pagina a sé, raggiungibile dalla schermata 1.
  impostazioni: {
    aperta: false,
    caricamento: false,
    caricate: false,
    errore: "",
    // Solo presenza, provenienza e coda di quattro caratteri: il valore della
    // chiave dal servizio locale non torna mai indietro.
    chiave: { presente: false, origine: "", coda: "" },
    percorsoChiave: "",
    valori: {},
    predefinite: {},
    // Le dichiarazioni «questi due codici sono lo stesso articolo». Vivono qui
    // perché è qui che si rileggono, e non dipendono dal confronto: valgono per
    // sempre e la pagina si apre anche senza.
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
    // La chiave appena incollata vive soltanto qui, e soltanto per il tempo che
    // passa fra l'incollarla e l'inviarla. Non entra mai nell'HTML della pagina:
    // il campo la riceve come proprietà del nodo, che nel sorgente non compare.
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

// ⚠ `useGrouping: "always"`. Il predefinito italiano di `Intl` non raggruppa le
// migliaia sotto le cinque cifre, quindi nella stessa colonna si leggevano
// «1264,20 €» e «12.345,68 €»: due formati per lo stesso tipo di numero, e a
// colpo d'occhio il primo si scambia per un ordine di grandezza in meno.
const euros = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "EUR",
  minimumFractionDigits: 2,
  useGrouping: "always",
});

const integers = new Intl.NumberFormat("it-IT", {
  maximumFractionDigits: 0,
});

// I modelli si pagano in dollari, non in euro: il listino di OpenRouter è in
// dollari e convertirlo qui vorrebbe dire inventare un cambio.
const dollars = new Intl.NumberFormat("it-IT", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

// "3 agosto": giorno e mese per le domande sugli ordini non ancora ricevuti.
const dayAndMonth = new Intl.DateTimeFormat("it-IT", {
  day: "numeric",
  month: "long",
});

// "lunedì 11 agosto": la data del confronto che si sta ancora guardando. Il
// giorno della settimana non è un vezzo — è il modo in cui una persona ricorda
// quando ha caricato i listini — e serve anche a tenere italiane le frasi che
// la contengono: si scrive "di lunedì 11 agosto", mentre "del 11 agosto" no.
const weekdayDayMonth = new Intl.DateTimeFormat("it-IT", {
  weekday: "long",
  day: "numeric",
  month: "long",
});

// "19 agosto 2026": la data della versione che sta girando. Qui l'anno serve,
// al contrario che nelle altre date: un programma rimasto indietro di mesi e
// uno rimasto indietro di un anno vanno distinti a colpo d'occhio, ed e'
// esattamente il caso che questa riga esiste per far vedere.
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

// A differenza di finiteNumber(), qui l'assenza del dato (null/undefined) resta
// assenza: serve per campi facoltativi calcolati dal backend (es. lastUnitPrice,
// suggestedQuantity) dove "non disponibile" e "zero" hanno significati diversi.
function optionalNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function userFacingText(value) {
  return String(value ?? "")
    // La parola sostituita cambia genere e numero, quindi l'articolo apostrofato
    // che la precede va sciolto insieme a lei: senza questa riga in pagina
    // compare "nell'controlli". Vale per l'unico caso che il programma produce
    // davvero; se in futuro se ne aggiungono altri vanno trattati qui.
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

// ⚠ Il caso normale non porta distintivo. «EAN esatto» e «Espositore identico»
// vogliono dire che tutto è andato come doveva, e su venti prodotti con quattro
// fornitori facevano ottanta bolli verdi per schermata — tutti a dire che la
// normalità è normale. Nello stesso riquadro il verde è anche il modo in cui si
// riconosce l'offerta scelta: la cosa da trovare a colpo d'occhio aveva lo
// stesso colore delle tre che non contavano. Il verde torna a voler dire una
// cosa sola. Il testo non sparisce — resta accanto al nome del fornitore, in
// grigio, perché dice COME quella riga è stata abbinata — ma smette di gridare.
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
    // Il nome del prodotto e del fornitore: il servizio li manda con ogni
    // controllo non superato, ed è l'unico modo di ritrovare la riga fra
    // cinquecento.
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
  // ⚠ Qui c'era `Math.max(1, …)`, e trasformava «non lo so» in «1 pezzo per
  // collo»: la scheda stampava quel numero come se l'avesse letto nel listino,
  // mentre la riga proposta non lo dice affatto. Zero vuol dire che manca, ed è
  // esattamente la ragione per cui il servizio locale non lascia accettare
  // quella riga: senza pezzi per collo l'ordine non si può calcolare.
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
    // Il motivo per cui l'analisi automatica ha scartato questa riga — «è una
    // confezione da 2 pezzi, mentre l'articolo cercato è singolo». ⚠ Era l'unica
    // cosa che serviva per rispondere, ed era l'unica che questa funzione non
    // copiava: la scheda chiedeva «è lo stesso?» mostrando codice e prezzo, e
    // al posto del motivo offriva il punteggio di somiglianza, che a chi ordina
    // non dice niente.
    rationale: String(candidate.rationale ?? ""),
    available: candidate.available === true,
  };
}

// Ordini in sospeso: l'abbinamento con i prodotti è deciso dal servizio locale
// sull'EAN (gli identificativi "product:<riga>" cambiano a ogni export del
// gestionale). Qui i dati arrivano già abbinati e vengono soltanto mostrati.
function normalizePendingOrder(entry) {
  return {
    orderId: String(entry?.orderId ?? entry?.order_id ?? ""),
    supplier: String(entry?.supplier ?? ""),
    supplierName: String(entry?.supplierName ?? entry?.supplier_name ?? entry?.supplier ?? "Fornitore"),
    createdAt: String(entry?.createdAt ?? entry?.created_at ?? entry?.orderedAt ?? entry?.ordered_at ?? ""),
    answeredAt: String(entry?.answeredAt ?? entry?.answered_at ?? ""),
    // Quando la domanda torna dopo un "non ancora arrivata": la data la decide
    // il servizio locale, qui si confronta soltanto con l'orologio.
    askAgainAt: String(entry?.askAgainAt ?? entry?.ask_again_at ?? ""),
    lineCount: Math.max(0, Math.trunc(finiteNumber(entry?.lineCount ?? entry?.line_count, 0))),
    totalNet: Math.max(0, finiteNumber(entry?.totalNet ?? entry?.total_net, 0)),
  };
}

function normalizePendingOrders(list) {
  return asArray(list).map(normalizePendingOrder).filter((entry) => entry.orderId);
}

// Voce agganciata al singolo prodotto da GET /api/review: quantità già ordinata
// (in unità d'ordine, cioè colli) e data dell'ordine.
function normalizeProductPendingOrder(entry) {
  return {
    orderId: String(entry?.orderId ?? entry?.order_id ?? ""),
    supplier: String(entry?.supplier ?? ""),
    supplierName: String(entry?.supplierName ?? entry?.supplier_name ?? entry?.supplier ?? "Fornitore"),
    orderedAt: String(entry?.orderedAt ?? entry?.ordered_at ?? entry?.createdAt ?? entry?.created_at ?? ""),
    quantity: Math.max(0, Math.trunc(finiteNumber(entry?.quantity, 0))),
    // Unità del momento dell'ordine, non di questa settimana.
    unit: String(entry?.unit ?? ""),
    // Quanti articoli del confronto condividono lo stesso codice a barre: se
    // più di uno, la quantità non è attribuibile a QUESTO articolo e la
    // frase deve dirlo (revisione avversariale R4).
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
    // ⚠ Il messaggio della soglia l'omaggio lo nomina già quasi sempre («…= 1
    // cartone di RESALINA SALE LAVASTOVIGLIE KG1 in omaggio: ne hai 49 in
    // tutto…»): la coda lo scriveva una seconda volta nella stessa frase.
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

// Che cosa il programma ha buttato via leggendo i listini. Il dato esiste già
// in review_data.json (auditSummary, scritto dalla catena del ricalcolo) e
// finiva nel nulla: normalizeReview lo scartava, e le righe non ordinabili, le
// escluse dal filtro e i codici a barre ripetuti si potevano vedere solo
// aprendo l'audit sul disco. Qui non si inventa niente: si legge quello che
// c'è. Uno scarto che non si conta non è una scelta, è una perdita di dati.
// I nomi dei fornitori arrivano con la mappa del PAYLOAD che si sta
// normalizzando: `supplierName()` legge `state.review`, che a questo punto è
// ancora la review precedente (o `null` alla prima apertura) — il riquadro
// rendeva «larice» e «noce» al posto dei nomi veri (revisione
// avversariale R4).
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
  // I prodotti che la pagina ha dovuto riassegnare da sola perché il fornitore
  // salvato non ha più un'offerta disponibile. La riassegnazione va bene; farla
  // in silenzio no: chi aveva scelto CIPRESSO a mano si ritroverebbe BETULLA
  // senza che niente glielo dica.
  const supplierReassignments = [];
  const products = asArray(payload.products).map((product, productIndex) => {
    const id = String(product.id ?? product.productId ?? `product-${productIndex + 1}`);
    const itemType = String(product.itemType ?? product.item_type ?? "product").toLowerCase();
    const normalizedItemType = itemType === "display" || itemType === "expositor" || itemType === "espositore" ? "display" : itemType === "kit" ? "kit" : "product";
    const quantity = Math.max(0, finiteNumber(product.quantity ?? product.orderQuantity ?? product.order_quantity, 0));
    const offers = asArray(product.offers).map((offer, offerIndex) => {
      const supplierId = String(offer.supplierId ?? offer.supplier_id ?? "");
      // Per gli espositori la quantità indicata nel listino è quasi sempre
      // un espositore, mentre il numero di pezzi contenuti è separato.  Dare
      // precedenza a quel numero evita di mostrare, erroneamente, "1 pezzo
      // ciascuno" e di calcolare un prezzo al pezzo uguale al prezzo intero.
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
        // Il no già dato su questa riga, quando c'è: lo mette il servizio
        // leggendo il magazzino delle conferme. Senza portarlo fin qui la
        // pagina direbbe di quel fornitore «non ce l'hanno nel listino di
        // adesso», che è falso — la riga ce l'hanno, è l'utente ad aver detto
        // che è un altro articolo — e non ci sarebbe nessun posto da cui
        // tornare indietro.
        rifiutata: offer.rifiutata && typeof offer.rifiutata === "object" ? {
          since: String(offer.rifiutata.since ?? ""),
          description: String(offer.rifiutata.description ?? ""),
        } : null,
        // La conferma può essere richiesta dalla singola offerta, non solo dal
        // prodotto: se non la si porta fin qui, spostando un prodotto su
        // un'offerta che la richiede il salvataggio viene rifiutato dal
        // servizio locale e nella pagina non compare nessuna casella per darla.
        requiresConfirmation: Boolean(offer.requiresConfirmation ?? offer.requires_confirmation),
        confirmationMessage: String(offer.confirmationMessage ?? offer.confirmation_message ?? ""),
        matchStatus: friendlyMatchStatus(offer.matchStatus ?? offer.match_status ?? "Offerta disponibile"),
        compositionStatus: String(offer.compositionStatus ?? offer.composition_status ?? ""),
        warning: String(offer.warning ?? ""),
        promotions: promotionItems,
        promotion: promotionItems.map(promotionText).filter(Boolean).join(" · ") || legacyPromotion,
        // Calcolati dal backend rispetto all'ultimo prezzo pagato (product.lastUnitPrice);
        // il frontend li mostra così come sono, senza ricalcolarli.
        lastPriceDifference: optionalNumber(offer.lastPriceDifference ?? offer.last_price_difference),
        lastPriceDifferencePct: optionalNumber(offer.lastPriceDifferencePct ?? offer.last_price_difference_pct),
      };
    });

    let selectedSupplierId = String(product.selectedSupplierId ?? product.selected_supplier_id ?? "");
    if (!offers.some((offer) => offer.supplierId === selectedSupplierId && offer.available)) {
      // Il fornitore più conveniente si sceglie sempre sul prezzo al pezzo, mai sul
      // totale in colli: colli di taglie diverse (es. 6 pezzi vs 24 pezzi) non sono
      // confrontabili sul totale.
      const precedente = selectedSupplierId;
      selectedSupplierId = offers
        .filter((offer) => offer.available)
        .sort((a, b) => a.pricePerPiece - b.pricePerPiece)[0]?.supplierId || "";
      // Si conta solo chi aveva davvero un fornitore scelto e adesso ne ha un
      // altro: un prodotto mai assegnato non è stato «spostato» da nessuno.
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

    // suggestedQuantity/quantitySource arrivano dal gestionale: finché la quantità non è
    // stata toccata dall'utente, la scheda mostra un piccolo indicatore "valore dal gestionale".
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
      // Ultimo prezzo netto pagato per pezzo (facoltativo, calcolato dal backend).
      lastUnitPrice: optionalNumber(product.lastUnitPrice ?? product.last_unit_price),
      orderUnitLabel: String(product.orderUnitLabel ?? product.order_unit_label ?? (itemType === "display" ? "espositori" : "colli")),
      selectedSupplierId,
      confirmed: Boolean(product.confirmed),
      requiresConfirmation: Boolean(product.requiresConfirmation ?? product.requires_confirmation),
      // Il motivo si legge sempre, quindi deve dire che cosa guardare: «conferma
      // la corrispondenza» chiedeva di spuntare senza dire in base a che cosa.
      confirmationMessage: String(product.confirmationMessage ?? product.confirmation_message ?? "Il codice a barre non coincide: confronta nome e formato con quello qui sotto."),
      // La conferma già registrata nel magazzino: chi, da quando, su quale
      // articolo. Sola lettura — non entra mai in `snapshot()`, che manda al
      // servizio soltanto `confirmed` — e assente finché una risposta non è
      // stata data davvero.
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
      // Solo decorazione informativa in sola lettura: non entra mai in snapshot()
      // e non cambia quantità, prezzi o fornitore scelto.
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
      // Da quale ricalcolo viene questo confronto. Serve a sapere quando le
      // colonne mostrate nelle schede dei documenti vanno riprese: cambiano
      // solo quando cambia la run che le ha usate.
      pipelineRunId: String(payload.run?.pipelineRunId ?? payload.run?.pipeline_run_id ?? ""),
    },
    files: asArray(payload.files ?? payload.sources).map((file, index) => ({
      id: String(file.id ?? `file-${index + 1}`),
      name: String(file.name ?? file.filename ?? `Documento ${index + 1}`),
      kind: userFacingText(file.kind ?? file.type ?? "Listino"),
      supplier: String(file.supplier ?? file.supplierName ?? "Da riconoscere"),
      // ⚠ Manca dal 20 agosto 2026 al 23, e con lui mancava un comando: la
      // scheda del documento chiama `renderColonnaDellOrdine(file, ...)`, che
      // disegna «Cambia colonna» soltanto se `file.supplierId` c'e'. Qui non
      // veniva copiato, quindi era `undefined` per tutti i documenti e il
      // pulsante non compariva mai — mentre la riga sopra di lui, «L'ordine
      // viene scritto nella colonna C», compariva eccome. Il servizio quel
      // campo lo manda; era questa funzione a buttarlo via.
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
    // Gli scarti della lettura dei listini e i totali del Riepilogo: due dati
    // che il servizio locale manda già e che la pagina buttava via.
    discardedRows: normalizeDiscardedRows(
      payload.auditSummary ?? payload.audit_summary,
      new Map(suppliers.map((supplier) => [supplier.id, supplier.name])),
    ),
    orderSummary: normalizeOrderSummary(payload.orderSummary ?? payload.order_summary),
    // Gli sconti di testata che il servizio ha già applicato ai prezzi, in
    // percentuale, per il solo campo che li mostra.
    // ⚠ Erano l'unica cosa che il servizio mandava dentro `payload.state`, e
    // questa funzione `state` non lo copiava: `state.review.state` non è mai
    // esistito. Il campo si ridisegnava vuoto a ogni rilettura — cioè subito
    // dopo averlo scritto — e sembrava che lo sconto non fosse stato preso,
    // mentre i prezzi erano già scontati ovunque.
    supplierDiscounts: normalizeSupplierDiscounts(payload.state?.supplierDiscounts),
    // Le coppie di codici a barre dichiarate lo stesso articolo. Valgono per
    // sempre e su tutti i fornitori: si vedono e si tolgono dal visualizzatore
    // dei listini, che è dove si fanno.
    uguaglianze: asArray(payload.uguaglianze).map((voce) => ({
      codici: asArray(voce?.codici).map((codice) => String(codice)),
      motivo: String(voce?.motivo ?? ""),
      dal: String(voce?.valida_dal ?? ""),
    })).filter((voce) => voce.codici.length === 2),
    supplierReassignments,
  };
}

// Percentuali, non frazioni: il servizio le manda già moltiplicate per cento,
// perché è così che si scrivono nel campo dove si digita «6».
function normalizeSupplierDiscounts(value) {
  if (!value || typeof value !== "object") return {};
  const sconti = {};
  for (const [supplierId, percento] of Object.entries(value)) {
    const numero = finiteNumber(percento, 0);
    if (numero > 0) sconti[String(supplierId)] = numero;
  }
  return sconti;
}

// «3 prodotti erano assegnati a CIPRESSO: ora sono passati a BETULLA.»
// Raggruppa per coppia partenza→arrivo, perché è la coppia che l'utente
// riconosce, non il singolo prodotto.
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

// «1 controlli», «1 offerte», «1 pezzi per collo»: il plurale sbagliato e' la
// firma di un programma scritto in fretta, e questa pagina ne aveva tre.
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
  // Chiamata subito dopo ogni sostituzione di state.review: eventuali avvisi di
  // cambio fornitore lasciati da una scheda precedente non hanno più senso.
  for (const timer of state.supplierChangeTimers.values()) window.clearTimeout(timer);
  state.supplierChangeNotices.clear();
  state.supplierChangeTimers.clear();
  // Vale anche per lo spostamento fra fornitori: il preventivo era calcolato sui
  // prodotti di prima e l'annulla ripristinerebbe assegnazioni non più valide.
  state.supplierMove = emptySupplierMove();
  state.supplierMoveUndo = null;
  // ⚠ E vale per l'«Annulla» dell'esclusione, che invece restava. Dopo un
  // ricalcolo la barra «… è stato escluso. [Annulla]» era ancora lì e rimetteva
  // fornitore e conferma DEL CONFRONTO PRECEDENTE su un prodotto del confronto
  // nuovo, che nel frattempo poteva avere offerte e prezzi diversi.
  state.exclusionUndo = null;
  // Stessa ragione per il «×» del riepilogo e per l'azzeramento in blocco: le
  // quantità salvate erano quelle del confronto di prima.
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
    // Due forme, e la vecchia va letta lo stesso: prima qui c'era il solo
    // elenco degli identificativi, e chi aggiorna il programma con dei prodotti
    // già esclusi ha ancora quella nel browser. Un elenco senza le scelte non è
    // un errore: vuol dire che di quei prodotti non sappiamo la quantità di
    // prima, ed è esattamente com'era il programma fino a ieri.
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
    // La mancata disponibilità della memoria del browser non deve bloccare l'ordine.
  }
}

function isExcluded(product) {
  return state.excludedProductIds.has(product.id);
}

// ⚠ Le sezioni apribili si richiudevano da sole, e non era un mistero: mentre
// il ricalcolo gira la pagina si ridisegna **ogni secondo**
// (RITMO_PIPELINE_MS), e `render()` non aggiorna il contenuto — lo sostituisce
// (`appElement.innerHTML = …`). L'apertura di un <details> vive nel nodo, non
// nel testo che l'ha prodotto: il nodo aperto veniva buttato e ne nasceva uno
// nuovo, chiuso. Chi apriva «Dettagli del confronto» — allora «del ricalcolo» — se lo vedeva richiudere in
// mezzo secondo — cioè in media metà del giro di polling (15 agosto 2026).
//
// La memoria sta in una mappa sola, con una chiave per riquadro: nove
// bandierine separate sarebbero nove occasioni di dimenticarsene una. Le
// chiavi che dipendono da un prodotto lo portano dentro, così due schede
// diverse non si aprono e chiudono insieme.
//
// ⚠ `apertoDiDefault` è per i riquadri che nascono APERTI — l'elenco dei
// prodotti di un fornitore nel riepilogo, che si richiude per scelta di chi
// ordina. Per quelli la mappa deve ricordare anche il «chiuso»: finché
// `ricordaApertura` cancellava la chiave, un riquadro aperto di suo tornava
// al valore di partenza al primo ridisegno, cioè si riapriva sotto le dita —
// lo stesso difetto del 15 agosto, preso dall'altro verso. Perciò adesso si
// scrive `false`, e «mai toccato» (`undefined`) resta una cosa diversa da
// «chiuso da chi ordina».
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

// Nessun fornitore può servire questo prodotto: non è «l'utente non ha ancora
// scelto», è «non c'è niente da scegliere». È la stessa domanda che il servizio
// locale si fa in `nessuna_offerta_utilizzabile` (server.py), e va fatta con le
// stesse parole: una quantità senza fornitore è uno stato valido, che alla
// compilazione finisce nell'elenco «Prodotti da reperire».
function nessunaOffertaUtilizzabile(product) {
  return !asArray(product?.offers).some((offer) => offer.available);
}

// Quanti fornitori sono fuori da questo prodotto per una tua risposta, non
// perché non ce l'abbiano. Si contano i fornitori, non le righe: un listino che
// porta due volte lo stesso articolo non deve contare due volte.
function rifiutatiDaTe(product) {
  return new Set(
    asArray(product?.offers)
      .filter((offer) => !offer.available && offer.rifiutata)
      .map((offer) => offer.supplierId),
  ).size;
}

// Le due domande aperte sull'abbinamento, che sono la stessa attività — dire se
// la merce è quella: «è lo stesso prodotto?» sulla riga che un fornitore
// propone, e la spunta di conferma sull'offerta scelta. Prima stavano in un
// filtro che si chiamava «Da verificare» e prendeva dentro qualunque avviso,
// compresi i prodotti che nessuno ha: due elenchi diversi sotto lo stesso nome.
function daConfermare(product) {
  if (confirmationRequired(product) && !product.confirmed) return true;
  return asArray(product?.offers).some(
    (offer) => offer.rejectedCandidate?.candidateKey && !offer.candidateDecision,
  );
}

// L'utente inserisce COLLI, non pezzi: nessun arrotondamento, nessuna eccedenza.
// Vale per ogni tipo di articolo (prodotto, espositore, kit) e per ogni fornitore.
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
    // Il prezzo al pezzo del listino, quando c'è, e non il collo diviso i
    // pezzi: 16,74 € / 12 fa 1,3949999… e si scriveva 1,39, mentre il listino
    // dice 1,395 e la colonna «Prezzo per pezzo» di pagina 2 scriveva 1,40
    // (KIF CANDEGGINA SPRAY 650ML, LARICE, 23 agosto 2026). Lo stesso numero
    // con cui il fornitore è stato scelto, dappertutto.
    pricePerPiece: finiteNumber(offer?.pricePerPiece) > 0
      ? finiteNumber(offer.pricePerPiece)
      : (factor > 0 ? unitPrice / factor : 0),
  };
}

// Il prezzo di un pezzo nel riepilogo di pagina 3, accanto al totale della
// riga e in grigio. Sotto l'intestazione «Prezzo al pezzo» quando le colonne
// ci stanno; sugli schermi stretti l'intestazione non si allinea più e sparisce
// (come in `.offer-grid__head`), e allora la parola accanto al numero si vede.
// Come là, l'intestazione è `aria-hidden` e la parola c'è sempre: chi legge
// con un lettore di schermo sente «1,67 € al pezzo», non una riga in più.
function renderPrezzoAlPezzo(calculation) {
  if (!calculation) return '<span class="order-lines__pezzo"></span>';
  return `<span class="order-lines__pezzo">${formatEuro(calculation.pricePerPiece)}<span class="order-lines__pezzo-etichetta"> al pezzo</span></span>`;
}

// Il fornitore più conveniente si sceglie SEMPRE sul prezzo al pezzo, mai sul totale in
// colli: colli con un numero di pezzi diverso non sono confrontabili sul totale.
// Confronta il prezzo al pezzo di un'offerta con l'ultimo prezzo pagato per quel prodotto.
// I campi lastPriceDifference/lastPriceDifferencePct sono calcolati dal backend quando
// disponibili; in loro assenza si ripiega su un confronto client-side equivalente.
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
    // La versione da cui questa scheda è partita: se sul disco ce n'è una più
    // nuova, il salvataggio viene rifiutato invece di cancellare in silenzio
    // quello che ha scritto l'altra scheda.
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
      // Facoltativo: permette al servizio locale di ricordare che l'utente ha
      // già toccato la quantità, così l'indicatore "valore dal gestionale" non
      // ricompare dopo un ricaricamento.
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
  // Una risposta che dichiara di essere JSON e non lo è non è una risposta
  // vuota. Trattarla come `{}` faceva uscire da /api/review un confronto di
  // «0 prodotti» — indistinguibile da un confronto davvero vuoto — invece di un
  // errore leggibile (revisione del 14 agosto 2026).
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
    // Il corpo della risposta viaggia con l'errore: dentro ci sono i controlli
    // non superati **uno per uno**, che il servizio manda apposta e che la
    // pagina buttava via — chi aveva dodici prodotti fermi ne leggeva tre e
    // «e altri 9», senza modo di trovarli.
    errore.dettagli = typeof body === "object" && body ? body : null;
    throw errore;
  }
  if (illeggibile) {
    throw new Error(`Il servizio locale ha risposto in un modo che non si riesce a leggere (${url}).`);
  }

  if (!contentType.includes("application/json")) {
    throw new Error("Il servizio locale ha restituito una risposta non valida.");
  }

  // ⚠ Qui, e non nei singoli gestori. Ogni risposta che porta una versione
  // nuova dello stato la consegna alla scheda: se la scheda resta indietro di
  // uno, il salvataggio dopo — fatto da LEI — si sente rispondere «un'altra
  // scheda ha salvato dopo di te», e l'utente va a cercare una scheda che non
  // esiste. Andava già fatto per il salvataggio e la compilazione, ma erano
  // elencati a mano: l'abbinamento, il rifiuto e lo sconto avanzavano la
  // versione e nessuno la raccoglieva (difetto del 26 agosto 2026). Raccolta
  // qui, un gestore nuovo non può più dimenticarsene.
  //
  // ⚠ Dopo il controllo di `response.ok`, non prima: una risposta d'errore
  // porta la versione di chi ha scritto per davvero, e raccoglierla vorrebbe
  // dire che il tentativo successivo passa il controllo e cancella quello che
  // l'altro ha appena scritto. Il rifiuto deve restare un rifiuto.
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
    // Chi riapre la pagina direttamente sul riepilogo deve vedere lo storico
    // senza dover uscire e rientrare dal passo.
    if (state.currentStep === 3) loadCompilazioni();
    caricaColonneDeiDocumenti();
  } catch (error) {
    state.loading = false;
    state.review = null;
    state.runtimeError = error.message;
    render();
  }
}

// Quali colonne il confronto vivo ha letto in ogni documento. Si ricarica quando
// cambia il ricalcolo di provenienza — è l'unica cosa che le fa cambiare — e mai
// durante il polling della catena, che ridisegna la pagina ogni secondo.
// Se la chiamata non riesce, le schede dei documenti restano come prima: è un
// riquadro che spiega, non un requisito per lavorare.
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

// Ordini della settimana scorsa non ancora confermati come ricevuti.
// Svuota documenti e confronto e riporta alla pagina 1, per cominciare la
// settimana da capo.
//
// ⚠ Quello che **non** manda via lo decide il servizio (`nuova_comparazione`),
// non questa funzione: conferme, schemi imparati, ordini in attesa e
// compilazioni già fatte restano. Qui si azzera solo quello che vive nella
// pagina — filtri, riepilogo, esito della compilazione, barre di annullamento —
// perché `loadReview()` quella roba non la tocca, e senza azzerarla la
// comparazione nuova nascerebbe con i filtri della settimana prima addosso.
async function cominciaNuovaComparazione() {
  if (state.nuovaComparazione.inCorso) return;
  if (mode === "demo") {
    state.nuovaComparazione.chiedendo = false;
    showToast("Nell’esempio non c’è niente da svuotare.", "error");
    render();
    return;
  }
  // La stessa guardia che ha il servizio. Qui serve lo stesso: la risposta
  // arriva dopo, e nel frattempo il pulsante avrebbe detto di aver fatto.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: la comparazione nuova si comincia quando ha finito.", "error");
    return;
  }
  state.nuovaComparazione.inCorso = true;
  // ⚠ Il salvataggio in coda va spento PRIMA di chiedere lo svuotamento.  La
  // pagina salva 450 ms dopo l'ultima modifica: un salvataggio partito dopo lo
  // svuotamento parla di un confronto che non c'e' piu', il servizio lo rifiuta
  // — giustamente, non ricrea le quantita' della settimana scorsa — e in faccia
  // a chi ha appena cominciato la settimana nuova esce un riquadro rosso
  // «Salvataggio non riuscito» per una cosa che ha fatto lui apposta.
  window.clearTimeout(state.saveTimer);
  const c_eranoModificheDaSalvare = state.dirty;
  state.dirty = false;
  state.savedVersion = state.saveVersion;
  render();
  try {
    const esito = await requestJson(API.comparazioneNuova, { method: "POST", body: JSON.stringify({}) });
    state.nuovaComparazione = { chiedendo: false, inCorso: false };
    // Sul disco `state.json` non c'e' piu': se la scheda continuasse a
    // dichiarare la versione di prima, il primo salvataggio dopo lo svuotamento
    // si sentirebbe rispondere «un'altra scheda ha salvato dopo di te».
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
    // Lo stato della catena torna quello che manda il servizio — «in attesa»,
    // senza la fascia dei documenti cambiati: non c'è nessun confronto di
    // prima di cui dire che i prezzi sono ancora quelli.
    state.pipeline = { ...state.pipeline, stato: esito?.pipeline || null, chiesto: false, errore: "", contattoPerso: "", controlliPersi: 0 };
    await loadReview();
    // E la domanda della settimana scorsa, che adesso ha il suo momento.
    loadPendingOrders();
    showToast(esito?.message || "Comparazione nuova.");
  } catch (error) {
    state.nuovaComparazione.inCorso = false;
    // Non e' cominciata niente: i documenti e le scelte sono ancora al loro
    // posto, quindi quello che c'era da salvare va ancora salvato.
    if (c_eranoModificheDaSalvare) scheduleSave();
    showToast(error.message || "La comparazione nuova non è cominciata.", "error");
    render();
  }
}

// L'elenco è un aiuto, non un requisito: se la chiamata non riesce si resta senza
// domande in cima alla pagina e tutto il resto continua a funzionare.
async function loadPendingOrders() {
  if (mode === "demo" || state.history.loading) return;
  state.history.loading = true;
  state.history.errore = "";
  try {
    const payload = await requestJson(API.historyPending);
    state.history.pending = normalizePendingOrders(payload?.pending);
  } catch (error) {
    // Un elenco vuoto e una richiesta fallita davano la stessa immagine: il
    // riquadro non compariva. Sono due cose opposte — «non c'è niente in
    // sospeso» e «non sono riuscito a saperlo» — e confonderle fa riordinare
    // merce già ordinata che deve ancora arrivare.
    state.history.pending = [];
    state.history.errore = `Non sono riuscito a leggere gli ordini già fatti: ${error.message}`;
  } finally {
    state.history.loading = false;
    // La risposta arriva a pagina già disegnata: si ridisegna solo dove serve e
    // senza rubare il cursore a chi nel frattempo sta scrivendo nella ricerca.
    // Anche la pagina 1: da quando c'è «Inizia nuova comparazione», la
    // domanda «è arrivata la merce?» compare lì — è il momento in cui la si
    // fa davvero, cioè quando si comincia la settimana nuova.
    if (state.currentStep <= 2) rerenderPreservingFocus();
  }
}

// Lo storico delle compilazioni: si chiede entrando nel passo 3 e di nuovo dopo
// ogni compilazione riuscita, perché quella appena fatta ne è la prima voce.
// Un errore qui resta dentro il riquadro (state.compilazioni.errore) e non tocca
// state.runtimeError: lo storico è un aiuto per ritrovare i listini di prima,
// non una condizione per compilare, e un passo 3 rotto sarebbe un danno più
// grande dell'elenco mancante.
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
    // L'elenco vecchio non si tiene: mostrerebbe come presenti compilazioni che
    // nessuno ha appena riletto sul disco.
    state.compilazioni.elenco = [];
    state.compilazioni.errore = `Storico delle compilazioni non letto: ${error.message}`;
  } finally {
    state.compilazioni.inCorso = false;
    // La risposta arriva a pagina già disegnata: si ridisegna senza rubare il
    // cursore a chi nel frattempo sta usando i filtri del riepilogo.
    rerenderPreservingFocus();
  }
}

// Lo sconto lo applica il servizio: i prezzi scontati e le assegnazioni nuove
// arrivano dalla rilettura del confronto, non da un conto rifatto qui. Un
// secondo motore di prezzi nel browser vorrebbe dire due verità sulla stessa
// riga, e la seconda sarebbe quella scritta accanto al totale.
async function applicaScontoFornitore(supplierId, valore) {
  if (!supplierId || mode === "demo") return;
  const percent = Math.min(99, Math.max(0, finiteNumber(valore, 0)));
  try {
    // ⚠ Il salvataggio PRIMA della richiesta, per la ragione scritta per
    // esteso in `abbinaLaRiga`: qui sotto `loadReview()` rilegge il confronto
    // e SOSTITUISCE quello in pagina, e una quantità scritta meno di 450 ms fa
    // — o rimasta indietro dopo un salvataggio fallito — se ne andrebbe con
    // lui, cioè un ordine sbagliato senza che niente lo dica (6 settembre
    // 2026). Con lo stato pulito `saveState()` torna vero subito.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const esito = await requestJson(API.supplierDiscount, {
      method: "POST",
      body: JSON.stringify({ supplierId, percent }),
    });
    await loadReview();
    // Il servizio conta quanti prodotti lo sconto ha spostato su questo
    // fornitore, e finora quel numero non arrivava da nessuna parte: chi
    // scriveva la percentuale non aveva modo di sapere se aveva spostato
    // qualcosa o niente. Il nome si legge DOPO `loadReview`, che è il momento
    // in cui il confronto aggiornato è in mano.
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
    // ⚠ Il salvataggio PRIMA della richiesta, per la ragione scritta per
    // esteso in `abbinaLaRiga`: eliminata la compilazione la pagina rilegge
    // il confronto e SOSTITUISCE quello in pagina, e una quantità scritta meno
    // di 450 ms fa — o rimasta indietro dopo un salvataggio fallito — se ne
    // andrebbe con lui, cioè un ordine sbagliato senza che niente lo dica (6
    // settembre 2026). Con lo stato pulito `saveState()` torna vero subito.
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
  // ⚠ IN_ATTESA adesso vuol dire due cose: «non è mai partito niente» e
  // «confronto vecchio, i documenti sono cambiati». Nessuna delle due merita la
  // barra delle fasi — quella la spegne renderAvanzamentoPipeline() guardando
  // lo stato — e la seconda la dice la fascia di renderCambiamentoDocumenti(),
  // che legge `state.pipeline.stato`: qui lo stato si conserva in ogni caso, ed
  // è quello che permette alla fascia di comparire.
  state.pipeline.chiesto = Boolean(stato && stato !== "IN_ATTESA");
  if (stato === "IN_CORSO") pianificaControlloPipeline();
}

async function deleteUploadedList(name) {
  if (!name || state.uploadDeletion.deleting) return;
  // Togliere un listino mentre la catena gira cambierebbe il risultato che si
  // sta calcolando: il comando è già spento in pagina, qui non si passa.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: i listini si eliminano appena ha finito.", "error");
    return;
  }
  state.uploadDeletion.deleting = name;
  state.runtimeError = "";
  render();
  try {
    // ⚠ Il salvataggio PRIMA della richiesta, per la ragione scritta per
    // esteso in `abbinaLaRiga`: eliminato il listino la pagina rilegge il
    // confronto e SOSTITUISCE quello in pagina, e una quantità scritta meno di
    // 450 ms fa — o rimasta indietro dopo un salvataggio fallito — se ne
    // andrebbe con lui, cioè un ordine sbagliato senza che niente lo dica (6
    // settembre 2026). Con lo stato pulito `saveState()` torna vero subito.
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

// «Non è lo stesso articolo», e il ritorno indietro. È il gemello negativo
// della spunta di conferma, e passa dalla stessa memoria: il magazzino delle
// conferme (`app/conferme.py`), riga con `accettata` falsa. Per questo non tocca
// nessuna chiave nuova di `state.json` e non ha bisogno di sopravvivere al
// ricalcolo: sopravvive perché è legata all'ARTICOLO, non alla riga.
//
// ⚠ Il salvataggio viene PRIMA della risposta, come per il candidato: questo
// pulsante ricarica il confronto, e quello che l'utente ha scritto finora non
// deve perdersi per averlo premuto. Il salvataggio passa anche con la conferma
// mancante — `save_state` la esclude dai bloccanti — quindi non c'è il rischio
// che il pulsante si rifiuti di funzionare proprio nel caso per cui esiste.
// Il «sì» alla stessa domanda, e si dà nello stesso modo.
//
// ⚠ Fino al 22 agosto 2026 il sì era una **casella** e il no un **pulsante**:
// una domanda sola, due meccaniche. La casella metteva il valore nello stato e
// aspettava il salvataggio differito — nessun riscontro, nessuna attesa
// dichiarata, e se il salvataggio falliva la spunta restava lì a dire che la
// risposta era data. Il pulsante invece agisce, aspetta e lo dice.
//
// Qui **non cambia dove la risposta finisce**: resta `product.confirmed` nello
// snapshot, che `_ricorda_le_conferme` scrive nel magazzino al salvataggio, e
// resta la chiave che la compilazione controlla. Cambia che il salvataggio è
// immediato e atteso, come per il no — e che se non riesce, il sì torna
// indietro invece di restare disegnato.
async function confermaLAbbinamento(productId, confermato) {
  const answerKey = `${productId}:conferma`;
  if (!productId || state.matches.answering) return;
  const product = findProduct(productId);
  if (!product) return;
  const prima = Boolean(product.confirmed);
  if (prima === Boolean(confermato)) return;
  state.matches.answering = answerKey;
  product.confirmed = Boolean(confermato);
  // Marcato «da salvare» e salvato subito. ⚠ Il timer che `scheduleSave()`
  // lascia va annullato: aspetta 450 ms, e se il salvataggio qui sotto fallisce
  // e il ramo d'errore riporta indietro il flag, quel timer rispedirebbe il
  // valore appena annullato — cioè registrerebbe una risposta che l'utente ha
  // visto fallire.
  scheduleSave();
  window.clearTimeout(state.saveTimer);
  rerenderPreservingFocus();
  try {
    const saved = await saveState();
    if (!saved) throw new Error(state.runtimeError || "Le modifiche non sono state salvate.");
    // Il confronto riletto: la conferma appena scritta torna con la sua data,
    // che è quello che il riquadro mostra al posto della domanda.
    const refreshed = await requestJson(API.review);
    state.review = normalizeReview(refreshed);
    restoreExcludedProducts();
    showToast(confermato
      ? "Conferma registrata: vale per l’articolo, anche con il listino della settimana prossima."
      : "Conferma tolta: la domanda è di nuovo aperta.");
  } catch (error) {
    // ⚠ Si torna indietro sul flag. Lasciarlo com'è vorrebbe dire una spunta
    // che dice «risposto» sopra una risposta che non è arrivata da nessuna
    // parte — esattamente il difetto della casella.
    //
    // Sul prodotto che si ha già in mano, non su uno ricercato adesso: fra
    // l'inizio e qui il confronto può essere stato riletto — succede a ogni
    // ricalcolo che finisce — e `findProduct` risponderebbe su un altro
    // oggetto, o su niente. Se il confronto è cambiato questo ripristino non
    // serve più a nessuno, ed è innocuo; se non è cambiato, è l'unico che
    // funziona.
    product.confirmed = prima;
    showToast(`Risposta non registrata: ${error.message}`, "error");
  } finally {
    state.matches.answering = "";
    // ⚠ `rerenderPreservingFocus` e non `render`: questa risposta lascia la
    // scheda dov'è — il pulsante diventa «Cambio idea» e tiene la stessa
    // `focus-key` — quindi il fuoco si può rimettere dov'era. È il rilievo [15]
    // dell'onda 5: premere non deve buttare via il fuoco. I due gemelli che
    // rispondono col no usano `render()` per la ragione opposta: quelli
    // ricaricano il confronto e il pulsante premuto sparisce.
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

// ⚠ Detto l'ultimo no, il prodotto SPARISCE da sotto il dito: l'elenco «Da
// confermare» è il posto da cui si risponde alle domande, e un prodotto senza
// più nessun fornitore non è più una domanda, quindi esce dal filtro. Provato
// il 22 agosto 2026 su SUPERMICIONE: due no, la scheda non c'era più, e in
// pagina nessuna parola diceva dove fosse andata. Il conto dei filtri si era
// mosso — «Da confermare» da 2 a 1, «Nessuno ce l'ha» da 78 a 79 — e basta.
//
// Si dice dove sta adesso, e si dice qui: quando il posto cambia sotto gli
// occhi, la frase che lo spiega deve arrivare col cambiamento, non stare
// scritta prima da qualche parte nella scheda.
//
// ⚠ E il posto adesso è «Hai risposto no», non «Nessuno ce l’ha»: mandare a
// cercarlo nel filtro sbagliato è peggio che non dire niente.
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

// La risposta è unica per l'intero ordine di quel fornitore: le consegne
// parziali non esistono. Le risposte possibili sono tre — è arrivata, non
// ancora (e la domanda torna la settimana prossima), non arriverà più (e la
// domanda si chiude senza dire che la merce è arrivata). Sono informative: non
// toccano quantità, prezzi né fornitore scelto.
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
    // In tutti i casi la domanda non viene riproposta in questa sessione.
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

// La merce è arrivata: gli avvisi "già ordinato" di quell'ordine non servono più.
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
  // I totali del Riepilogo — e lo scarto di arrotondamento fra testata e righe —
  // li rifà il servizio locale a ogni salvataggio: il browser non è autorevole
  // sui prezzi e qui si limita a tenere quello che gli arriva.
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

// La versione nuova dello stato, presa da qualunque risposta la porti. La
// chiama `requestJson` su ogni risposta riuscita, una volta sola per tutti:
// l'elenco a mano di chi «riscrive tutto lo stato» ne dimenticava tre su cinque.
// Sale e basta — mai indietro — così l'ordine in cui arrivano non conta.
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
      // Tre tentativi e poi basta: un servizio spento non si convince
      // riprovando all'infinito, e una pagina che chiama ogni cinque secondi
      // per sempre e' un guasto suo. Dopo il terzo resta il comando in cima.
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
  // renderAlert disegna già la spunta per il tono "success" e il foglio di
  // stile ha già .alert--success: mancava solo il modo di arrivarci. Serve alla
  // pagina Impostazioni, dove "la chiave funziona" è la sola risposta verde.
  if (issue.severity === "success") return "success";
  return "warning";
}

// collectIssues() scorre tutti i prodotti ed è invocata fino a 40 volte per pagina:
// il risultato viene calcolato una sola volta per ciclo di render.
// INVARIANTE: chi modifica quantità, fornitore, conferme o esclusioni deve invalidare
// la cache, direttamente o passando da render(). Un consumatore che legge gli avvisi
// fuori dal ciclo di render deve chiamare invalidateIssues() prima.
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
    // ⚠ «Non hai ancora scelto» e «non c'è niente da scegliere» sono due cose
    // diverse, e questo riquadro le trattava come una sola: un prodotto che
    // nessun listino sa servire fermava la compilazione e chiedeva di mettere
    // a zero la quantità — cioè di cancellare proprio il numero che serve a
    // reperirlo altrove. Il servizio locale lo accetta dal 16 agosto 2026
    // (`nessuna_offerta_utilizzabile`, server.py): la quantità resta, il
    // prodotto non entra nei listini dei fornitori ed esce nell'elenco
    // «Prodotti da reperire». Sulla sua scheda lo dice già l'avviso
    // SENZA_OFFERTA_UTILIZZABILE, che porta il perché.
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

// ⚠ «Proposte da controllare — 9 prodotti hanno una proposta di un fornitore
// da confermare o rifiutare. Li trovi con il filtro "Da confermare".» Daniele,
// 20 agosto 2026: «sarebbe carino poter cliccare su "Vai al filtro" e essere
// rimandati nella sezione di controllo». L'avviso diceva dove andare e poi
// lasciava andarci a mano — cercare la voce giusta in un elenco a tendina,
// dopo aver cambiato pagina. Sulla pagina 3 pesa di piu': «Conferma richiesta ·
// bloccante» dice che la compilazione non parte e non da' la strada per
// sbloccarla.
//
// Il comando si decide dal `code`, che il servizio manda gia' e che i due
// avvisi nati nel browser adesso hanno anche loro: nessuna tabella di titoli da
// tenere allineata alle frasi. Un avviso senza codice noto non ha comando, e
// resta come prima.
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

// `conComando` e' esplicito e parte spento: gli stessi avvisi compaiono anche
// sulla scheda del singolo prodotto in pagina 2, dove il comando porterebbe
// dove si e' gia' — e dove il pulsante per rispondere sta due centimetri sotto.
// Lo accendono solo i riquadri di pagina, in `renderAvvisi`.
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

// Quanti avvisi uguali servono perché valga la pena contarli invece di
// stamparli. Due sono due righe; tre in su sono un muro.
const AVVISI_DA_RAGGRUPPARE = 3;

// ⚠ La pagina 3 stampava 8 riquadri bloccanti e 45 avvisi, uno per uno, prima
// del riepilogo: cinquantatré scatole da leggere per arrivare ai numeri
// dell'ordine. Ma i titoli erano quattro — «Conferma richiesta» otto volte,
// «Possibile prodotto CIPRESSO» trentanove — e a ripetersi era solo quello: il
// messaggio di ciascuno è diverso e serve.
//
// Si raggruppa per titolo, che è quello che il servizio ha già scritto: niente
// tabella di codici da tenere allineata, e un titolo nuovo si raggruppa da
// solo. Non si toglie niente: i messaggi stanno tutti dentro il gruppo.
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
  // Un gruppo bloccante non si chiude: è il motivo per cui la compilazione non
  // parte, e chiuderlo vorrebbe dire nascondere l'unica cosa da fare.
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
      // Due avvisi con lo stesso titolo sono due riquadri, ma il comando che
      // porta al filtro e' lo stesso: si scrive sul primo, non su tutti.
      : gruppo.voci.map((issue, indice) => renderAlert(issue, { conComando: indice === 0 })).join("")))
    .join("");
}

// Gli avvisi che riguardano i documenti di partenza — un listino non letto, un
// fornitore rimasto fuori dal confronto — devono arrivare prima che l'utente
// scelga. Fino alla Fase 9b comparivano solo nel riquadro del passo 3, cioè a
// prodotti e fornitori già scelti: un fornitore poteva mancare per un intero
// passo senza che niente lo dicesse.
function avvisiDelConfronto() {
  return (state.review?.warnings || []).filter((issue) => !issue.productId);
}

// Gli avvisi che riassumono i PRODOTTI, non i documenti: si rispondono in
// pagina 2, sull'elenco, ed e' li' che devono stare. Tutti gli altri parlano
// dei file caricati e stanno in pagina 1, dove i file si cambiano.
//
// ⚠ Un codice che questo elenco non conosce finisce fra quelli dei documenti,
// cioe' in pagina 1: e' la pagina che si vede sempre, quindi un avviso nuovo
// non sparisce mentre nessuno se ne accorge.
const AVVISI_DEI_PRODOTTI = new Set([
  "RIFIUTI_CON_CANDIDATO_FORTE",
  "PRODOTTI_SENZA_OFFERTA",
  "DECISIONI_AI_SCARTATE",
  "ORDINI_SCADUTI_SENZA_RISPOSTA",
]);

// ⚠ Lo stesso riquadro, con lo stesso titolo «Da sapere prima di continuare»,
// stava in cima alla pagina 1 e alla pagina 2 con dentro le stesse frasi, e in
// pagina 3 ricompariva come «Avvisi». Daniele, 20 agosto 2026: ripetuti
// perdono peso, chi li vede tre volte smette di leggerli.
//
// Adesso ogni avviso sta dove si puo' fare qualcosa: i documenti in pagina 1,
// i prodotti in pagina 2 — dove ora c'e' anche il comando che porta al filtro
// giusto — e la pagina 3 li rivede tutti, perche' e' l'ultimo momento prima di
// scrivere i listini. I titoli sono tre, diversi, e ognuno dice di che cosa
// parla il riquadro che apre.
//
// Un avviso **bloccante** fa eccezione e resta su tutt'e due le pagine: ferma
// la compilazione, e non lo si nasconde per ordine.
// Gli avvisi del confronto che il riquadro in cima alla pagina 1 sta davvero
// stampando adesso. È l'insieme che serve a chi deve evitare di ripeterli, e
// non coincide con «tutti quelli del confronto»: mentre la procedura guidata è
// aperta restano solo i bloccanti.
function avvisiVisibiliInCima() {
  const soloBloccanti = schemaMappingRequired();
  return avvisiDelConfronto().filter(
    (issue) => issue.blocking || (!soloBloccanti && !AVVISI_DEI_PRODOTTI.has(issue.code)),
  );
}

function renderSourceWarnings(pagina) {
  const deiProdotti = pagina === 2;
  // ⚠ Mentre la procedura guidata è aperta la pagina ha una domanda sola — dove
  // sono le colonne — e questi avvisi parlano del confronto PRECEDENTE, che non
  // è quello che si sta rifacendo. Restano i bloccanti, che fermano la
  // compilazione e non si nascondono per ordine; gli altri tornano da soli
  // appena il ricalcolo riparte e riscrive `review_data.json`.
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
    // ⚠ run.status vale "ready" già al primo avvio, con zero documenti caricati:
    // il badge verde «Dati pronti» diceva quindi che era tutto pronto a chi non
    // aveva ancora fatto niente. Verde solo quando un confronto esiste davvero.
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

// ⚠ La spunta verde diceva «fatto» e voleva dire «ci sei passato»: era
// `state.currentStep > step.id`, cioe' un fatto di sola posizione. Bastava
// premere «2. Scegli» — premibile appena il servizio risponde, anche con zero
// listini caricati — perche' il passo 1 si dipingesse di verde con la spunta
// senza che fosse stato importato niente. All'inverso, tornando dalla pagina 3
// alla 1 i passi 2 e 3 perdevano la spunta pur avendo quantita' e fornitori
// scelti. Per una persona non tecnica il verde con la spunta E' l'affermazione
// che quel passo e' a posto.
//
// Adesso lo stato di ogni passo lo dicono i fatti che il programma conosce
// gia': il passo 1 e' fatto quando un confronto esiste, il passo 2 quando c'e'
// almeno un prodotto con una quantita'. Il passo 3 non ha un «fatto» da
// dichiarare — quello che si fa li' e' compilare, e la compilazione ha il suo
// riquadro — quindi resta senza. Lo stesso mestiere lo fa gia' il distintivo in
// alto, che non diventa verde finche' un confronto non esiste davvero.
function passoCompletato(id) {
  if (id === 1) return confrontoDisponibile();
  if (id === 2) return orderedProducts().length > 0;
  return false;
}

function renderStepper() {
  // ⚠ Le Impostazioni sono una quarta pagina lunga cinque riquadri, e la barra
  // dei passi — che sta fuori da `#app` e non viene ridisegnata da chi la apre —
  // continuava a dichiarare attiva la pagina da cui si era entrati. Appena si
  // scorre, l'unica cosa ferma sullo schermo diceva «Importa i dati» mentre si
  // stava guardando l'elenco delle uguaglianze dichiarate. Chi non e' tecnico
  // legge che si trova dove non e'. Adesso, mentre sono aperte, la barra ha una
  // quarta voce ed e' quella marcata: i tre passi restano premibili, e premerne
  // uno chiude le impostazioni, che e' quello che `goToStep()` fa gia'.
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
  // ⚠ Un salvataggio fallito lasciava l'avviso e nessun comando: `state.dirty`
  // restava vero, nessun altro tentativo veniva pianificato, e per ritentare
  // bisognava sapere che serviva toccare qualcos'altro. Chi chiudeva la pagina
  // in quel momento perdeva tutto il lavoro da li' in poi. Adesso il programma
  // riprova da solo per tre volte, e se non ce la fa lascia qui il comando.
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

  // Prima del controllo sulla revisione: le impostazioni non dipendono dai
  // prodotti, e il momento in cui servono davvero è proprio quello in cui la
  // fase AI non ha funzionato.
  if (state.impostazioni.aperta) {
    appElement.innerHTML = renderSettingsPage();
    // Il campo della chiave non ha mai il valore nell'HTML. Rimetterlo come
    // proprietà del nodo lo fa sopravvivere a un ridisegno — dopo la prova di
    // connessione si preme "Salva" senza reincollare — senza che il valore
    // compaia nel sorgente della pagina.
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
  // ⚠ La finestra della colonna d'ordine si apre dalla pagina 1, non dalla 2:
  // sta qui e non dentro un `renderX Step` perché le tre pagine hanno tre
  // radici diverse, e una finestra montata dentro una sola sparirebbe appena
  // si cambia passo — con lo stato ancora aperto e nessun modo di chiuderla.
  appElement.innerHTML = renderers[state.currentStep - 1]() + renderColonnaOrdineDialog();
}

// `extra` è per i comandi che non fanno parte del percorso — oggi solo
// «Impostazioni». Stavano in fondo, in fila con «Continua»: quattro comandi
// uno accanto all'altro e nessuno che dicesse quale fosse il passo successivo.
// La configurazione non è un passo del lavoro e sta dove si guarda una volta:
// in testa alla pagina, accanto alla data del confronto.
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

// Che cosa dire di un documento, senza dirlo due volte. ⚠ Il gestionale
// arriva con `kind` e `supplier` uguali («Gestionale» e «Gestionale») e la
// scheda scriveva la stessa parola due volte di fila.
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
  // Durante il ricalcolo togliere un listino cambierebbe il risultato che si sta
  // calcolando: il comando resta visibile, spento, e dice perché.
  const bloccato = pipelineInCorso();
  // ⚠ Il messaggio del documento è la motivazione tecnica con cui la catena ha
  // riconosciuto lo schema («Schema riconosciuto dal registro (larice_v1,
  // confidenza 0.97): nessuna chiamata AI»), identica su tutti e cinque i
  // documenti quando è andato tutto bene. Che il file sia a posto lo dice già
  // il badge «Pronto»: la motivazione serve solo quando qualcosa non torna, ed
  // è lì che va letta. Quello che di quel documento si vuole sapere sempre —
  // da quale colonna arriva il prezzo — sta nella tabella qui sotto.
  // ⚠ E nemmeno sui documenti che la procedura guidata sta chiedendo qui sotto:
  // «Letto. Le colonne verranno riconosciute al prossimo confronto; se non le
  // riconosce, te le chiede» dice il contrario di quello che sta succedendo —
  // te le sta chiedendo adesso, in fondo a questa stessa pagina.
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
// «Quali colonne legge» — su ogni documento, sempre
// --------------------------------------------------------------------------
// Finché questa tabella non c'era, l'assegnazione delle colonne si poteva
// vedere in un caso solo: quando il programma NON riconosceva un file e apriva
// la mappatura guidata. Sui documenti riconosciuti — cioè tutti, nella
// settimana normale — la pagina diceva «Schema riconosciuto dal registro» e
// nient'altro, e per sapere da quale colonna veniva il prezzo bisognava aprire
// il listino e contare le colonne a mano.
//
// I numeri arrivano dal servizio già risolti (`GET /api/schemas/columns`): qui
// non si indovina niente e non si rilegge nessun file. La colonna in cui il
// programma **scrive** l'ordine sta a parte, perché è l'unica che non legge.
function renderColonneDelDocumento(file) {
  const nome = String(file.name || "");
  const voce = state.colonneDocumenti.perNome[nome];
  const chiave = `colonne-documento:${nome}`;
  if (!voce) {
    if (state.colonneDocumenti.caricando || !state.colonneDocumenti.caricate) return "";
    // Un documento senza risposta non si nasconde: «non lo so» è
    // l'informazione, e tacere la farebbe sembrare una tabella dimenticata.
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

// Rivedere TUTTE le colonne, non solo quella d'ordine.
//
// ⚠ Il selettore delle colonne esiste dal primo giorno, ma si apre soltanto
// quando la catena si ferma su uno schema che il registro non conosce — e con i
// fornitori riconosciuti non si ferma mai. «Prima avevo il selettore manuale
// dove potevo confermare quale colonna contenesse quale dato, con anteprima,
// mentre ora non lo vedo più» (Daniele, 15 agosto 2026). Non era sparito: non
// c'era più nessuna porta per arrivarci.
//
// Sta qui, e non accanto a «Elimina», perché questo è il riquadro in cui si
// stanno già guardando le colonne: chi vede una riga sbagliata la corregge da
// dove l'ha vista.
function renderComandoRivediColonne(file) {
  const nome = String(file.name || "");
  if (!nome || mode === "demo") return "";
  const bloccato = pipelineInCorso();
  return `<button type="button" class="button button--ghost button--piccolo"
    data-action="apri-colonne" data-upload-name="${escapeHtml(nome)}"
    data-focus-key="colonne-documento-${escapeHtml(nome)}" ${bloccato ? "disabled" : ""}
    title="${bloccato ? "Non ora: il confronto è in corso." : "Correggi dove stanno prezzo, codice, descrizione e pezzi per collo"}">Rivedi le colonne</button>`;
}

// La colonna che il programma **scrive**, non una di quelle che legge — e il
// modo di spostarla.
//
// ⚠ Fino al 18 agosto 2026 questa riga diceva soltanto dov'è. La colonna si
// sceglieva una volta sola, dentro la mappatura guidata, che si apre solo
// quando il programma non riconosce le colonne di un documento: per i quattro
// fornitori conosciuti restava quella scritta nel registro, e per spostarla
// bisognava aprire un file JSON.
//
// Il pulsante non compare quando il registro non dichiara nessuna colonna: lì
// non c'è una colonna da spostare, c'è una configurazione di scrittura da
// creare, e quella nasce dalla mappatura guidata insieme a tutto il resto.
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

// Dove il fornitore scrive le sue offerte — la stessa domanda delle altre
// colonne, e per questo sta qui dentro e non in un riquadro nuovo.
//
// ⚠ È il buco misurato su QUERCIA il 17 agosto 2026: un fornitore nuovo si legge
// e si compila, ma le sue offerte no, e in pagina non c'era una sola riga che
// lo dicesse. `commercialConditions: null` non è un vuoto da nascondere: è la
// differenza fra «questo fornitore non fa offerte» e «le fa e non gliele
// stiamo leggendo», e la seconda è una cosa da fare.
function renderOffertePerDocumento(file, voce) {
  if (isManagementFile(file)) return "";
  const condizioni = voce.commercialConditions;
  const colonna = condizioni?.fields?.text;
  if (!colonna || !colonna.lettera) {
    return `<p class="doc-columns__note doc-columns__note--manca">Le offerte scritte su questo listino <strong>non le legge nessuno</strong>: manca la colonna in cui il fornitore le scrive. Indicamela quando ti chiedo le colonne di questo documento.</p>`;
  }
  return `<p class="doc-columns__note">Le offerte del fornitore si leggono nella colonna <strong>${escapeHtml(String(colonna.lettera))}</strong>.</p>`;
}

// Il riquadro degli scarti: quante righe di ogni listino non sono entrate nel
// confronto e perché. Sta nella pagina dove l'utente guarda i suoi documenti,
// perché è lì che si chiede "il listino è entrato tutto?". I numeri arrivano
// dall'audit della run: qui non se ne calcola nessuno.
// I motivi arrivano dal servizio come etichette da programma («senza_prezzo»,
// «DISPLAY_COMPONENT»): qui diventano italiano. Un'etichetta che non è in
// tabella si mostra com'è — le etichette degli scarti dichiarati dal registro
// le scrive chi configura il listino, e inventarne una traduzione le
// nasconderebbe.
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
  // Il badge dice quello che c'è: «0 righe» sotto un titolo sugli scarti,
  // quando dentro ci sono solo codici ripetuti, erano tre notizie che non
  // stavano insieme (revisione avversariale R4).
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
            // «righe lette», non «entrate nel confronto»: le scartate stanno
            // DENTRO quel conteggio, e i due numeri messi in fila non si
            // sommerebbero (revisione avversariale R4).
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

// I documenti rimasti fuori, con il nome e il motivo. Ha un comando di
// rimozione tutto suo: `remove-file` passa l'indice dentro `state.pendingFiles`,
// e riusarlo qui toglierebbe un documento buono al posto di uno scartato.
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

// Il riquadro del ricalcolo: un pulsante, e poi le nove fasi che scorrono.
// Non decide niente da solo — stato, fasi, numeri e avvisi arrivano tutti dal
// servizio locale, che è l'unico che sa a che punto è la catena.
const TONO_FASE = {
  COMPLETATO: "success",
  IN_CORSO: "info",
  ERRORE: "danger",
  IN_ATTESA: "neutral",
};

function pipelineInCorso() {
  return String(state.pipeline.stato?.stato || "") === "IN_CORSO";
}

// ⚠ «IN_ATTESA» ha due significati e per un po' la pagina ne ha visto uno solo.
// Senza `cambiamento` vuol dire che non è mai partito niente: una barra a zero
// sarebbe rumore e non si disegna — quell'assunto resta giusto. Con
// `cambiamento` vuol dire invece che i documenti sono cambiati DOPO l'ultimo
// confronto: i prezzi delle pagine 2 e 3 sono vecchi, e va detto. Lo stato lo
// tiene il servizio locale, quindi la fascia torna anche dopo un ricaricamento
// e sparisce da sola quando il confronto è stato rifatto.
//
// Il campo può non esserci (servizio più vecchio della pagina): in quel caso si
// fa come prima e non si mostra niente.
function cambiamentoDocumenti(stato = state.pipeline.stato) {
  const cambiamento = stato?.cambiamento;
  if (!cambiamento || typeof cambiamento !== "object") return null;
  const tipo = String(cambiamento.tipo || "");
  if (tipo !== "eliminato" && tipo !== "caricato" && tipo !== "colonne") return null;
  const nomi = (elenco) => asArray(elenco).map((voce) => String(voce ?? "").trim()).filter(Boolean);
  return { tipo, documenti: nomi(cambiamento.documenti), fornitori: nomi(cambiamento.fornitori) };
}

// La data del confronto che si sta ancora guardando: è la sola cosa che rende
// concreta la frase «i prezzi sono vecchi».
function dataUltimoConfronto() {
  return formatWeekdayDayMonth(state.review?.run?.createdAt);
}

function elencoNomi(nomi) {
  if (!nomi.length) return "";
  if (nomi.length === 1) return nomi[0];
  return `${nomi.slice(0, -1).join(", ")} e ${nomi[nomi.length - 1]}`;
}

// Le due frasi della fascia: che cosa è cambiato, e che cosa vedi adesso.
// Si nominano i fornitori quando ci sono — «CIPRESSO» dice più di «LISTINO
// CIPRESSO VALIDO FINO AL 01-09-26.xlsx» — e i nomi dei file altrimenti.
function frasiCambiamentoDocumenti(cambiamento = cambiamentoDocumenti()) {
  if (!cambiamento) return null;
  const perNome = cambiamento.fornitori.length ? cambiamento.fornitori : cambiamento.documenti;
  const uno = perNome.length <= 1;
  // ⚠ «Hai caricato» su una mappatura corretta a mano sarebbe una bugia: il
  // documento è lo stesso, a cambiare è come lo si legge.
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

// La fascia non impedisce niente: dice che cosa è cambiato e lascia lavorare.
// Fuori dalla pagina 1 porta anche il comando per tornare dov'è il pulsante.
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
// Un solo pulsante primario per volta (pagina 1)
// --------------------------------------------------------------------------
// Prima erano tre — «Carica e controlla N documenti», «Ricalcola il confronto»,
// «Continua con N prodotti» — tutti blu e tutti attivi insieme, e nessuno
// diceva se toccasse a lui. Adesso lo stato dei documenti sceglie il primario;
// gli altri restano al loro posto, in secondario, e nessuno viene tolto di
// mano all'utente se non perché è davvero impossibile in quel momento.

function documentiCaricati() {
  return asArray(state.review?.files).length > 0;
}

// «Esiste un confronto» vuol dire che nelle pagine 2 e 3 c'è qualcosa da
// guardare. ⚠ run.status vale "ready" anche al primo avvio, con zero documenti:
// non distingue «pronto» da «non ancora fatto», e per un po' il badge in alto
// diceva «Dati pronti» a chi non aveva caricato niente.
function confrontoDisponibile() {
  return asArray(state.review?.products).length > 0;
}

// Che cosa manca per poter confrontare: la frase nomina il documento assente
// invece di limitarsi a spegnere il pulsante.
function documentiMancantiText() {
  const files = asArray(state.review?.files);
  const elenco = files.some(isManagementFile);
  const listini = files.some((file) => !isManagementFile(file));
  if (elenco && listini) return "";
  if (!elenco && !listini) return "Carica prima l’elenco dei prodotti e almeno un listino.";
  if (!elenco) return "Manca l’elenco dei prodotti da ordinare: caricalo qui sopra.";
  return "Manca almeno un listino fornitore: caricalo qui sopra.";
}

// Le otto situazioni della pagina 1, dalla più specifica alla più ordinaria.
// Il nome è quello che si legge nella tabella del piano.
function statoPaginaImporta() {
  // Il ricalcolo in corso viene prima di tutto, anche di documenti scelti e non
  // ancora inviati: quelli non si possono caricare finché la catena gira, e
  // dire «carica» mentre il caricamento è spento sarebbe una presa in giro.
  if (pipelineInCorso()) return "RICALCOLO_IN_CORSO";
  if (state.pendingFiles.length) return "FILE_SCELTI";
  if (schemaMappingRequired()) return "COLONNE_SCONOSCIUTE";
  if (String(state.pipeline.stato?.stato || "") === "ERRORE") return "RICALCOLO_FALLITO";
  if (cambiamentoDocumenti()) return "DOCUMENTI_CAMBIATI";
  if (documentiMancantiText()) return "NIENTE_CARICATO";
  if (confrontoDisponibile()) return "CONFRONTO_AGGIORNATO";
  return "PRONTI_MAI_CONFRONTATI";
}

// Il pulsante del ricalcolo. ⚠ «Ricalcola» la prima volta è una bugia: non c'è
// niente da ri-calcolare. Il testo dipende dallo stato, non è fisso.
function comandoConfronto(situazione = statoPaginaImporta()) {
  if (state.pipeline.avviando) {
    return { etichetta: "Avvio del confronto…", tono: "secondary", disabilitato: true, nota: "" };
  }
  if (situazione === "RICALCOLO_IN_CORSO") {
    // Nessun primario mentre la catena gira: la barra e le fasi dicono già a
    // che punto è, e un pulsante blu spento chiederebbe di premere e basta.
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
    // ⚠ Spento, non solo declassato.  Era premibile con una nota accanto, ed e'
    // il tranello del lunedi': si scelgono i listini nuovi, ci si dimentica di
    // premere «Carica», si confronta, e il confronto esce con i listini della
    // settimana prima senza che niente sembri andato storto — i prezzi vecchi
    // arrivano fino all'ordine.  La nota diceva la cosa giusta ed era testo
    // piccolo accanto a un pulsante che si poteva premere.  Chi ha scelto per
    // sbaglio non resta chiuso fuori: i documenti scelti si tolgono dall'elenco
    // qui sopra, e il comando torna.
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

// Il pulsante «Continua». ⚠ Non deve mai mentire: quando il confronto è più
// vecchio dei documenti resta cliccabile — bloccare il lavoro sarebbe peggio —
// ma dice su quale confronto sta per portarti.
// Quanti prodotti del confronto vengono dall'elenco del gestionale.
// ⚠ È il numero che deve tornare con quello scritto sulla scheda del
// gestionale («451 righe»): è così che si vede, senza aprire niente, che il
// documento caricato è quello giusto. Il totale dell'elenco è un altro numero —
// ci sono dentro gli espositori dei listini e i prodotti aggiunti a mano — e
// metterlo qui faceva sembrare sbagliato un documento che era giusto.
// La scomposizione completa sta in cima alla pagina 2.
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
  // ⚠ Due numeri, non uno: «457 prodotti» è quanto è grande il confronto,
  // «451 dal gestionale» è quello che deve tornare con le righe scritte sulla
  // scheda del documento. Finché c'era solo il primo, pagina 1 mostrava 451
  // sulla scheda e 457 qui sotto senza spiegare la differenza, e sembrava che
  // il programma avesse letto un elenco diverso da quello caricato.
  if (numeri.prodotti) {
    const dalGestionale = Number(numeri.prodottiGestionale) || 0;
    voci.push(dalGestionale && dalGestionale !== Number(numeri.prodotti)
      ? `${formatInteger(numeri.prodotti)} prodotti, di cui ${formatInteger(dalGestionale)} dal gestionale`
      : `${formatInteger(numeri.prodotti)} prodotti`);
  }
  // ⚠ «casi da valutare» e «decisi» stanno già, con più contesto, dentro le
  // fasi di «Dettagli del confronto»: qui erano due numeri in più su una riga
  // che si legge di sfuggita. «prodotti» invece resta, ed è voluto: è il
  // numero che deve tornare con le righe scritte sulla scheda del documento.
  if (numeri.spesaUsd) voci.push(`${Number(numeri.spesaUsd).toFixed(3)} $`);
  if (!voci.length) return "";
  return `<p class="file-card__meta">${voci.map((voce) => `<span>${escapeHtml(voce)}</span>`).join("")}</p>`;
}

function schemaMappingRequired(stato = state.pipeline.stato) {
  return String(stato?.fermata?.code || "") === "SCHEMA_SCONOSCIUTO";
}

// I documenti che la fermata degli schemi sta chiedendo di configurare adesso.
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

// L'id di un campo della procedura guidata, costruito con i due valori che nel
// markup ci sono gia': quale documento e quale campo. Serve al `for` della sua
// etichetta — senza, per un lettore di schermo quelle tendine non hanno nome, e
// si perde anche col mouse, perché cliccare l'etichetta non porta al campo.
// ⚠ Il prefisso «c-» non è decorazione: `profileId` può cominciare per una
// cifra, e un id che comincia per cifra è valido in HTML5 ma rompe i selettori
// CSS non scappati.
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

// I separatori di sezione che il profilo ha trovato in testa al documento, e
// quello scelto dall'utente. Su QUERCIA il listino vero comincia dopo l'unico
// `A68 = "LISTINO"`, dopo 56 righe che sono valorizzazioni di omaggi: scrivere
// 69 in «Prima riga dei prodotti» sarebbe un numero che la settimana prossima
// e' un altro numero, e nessuno se ne accorgerebbe.
function schemaSectionBreaks(foglio) {
  return (foglio.sectionBreaks || []).filter((voce) => Number(voce.row) > 0);
}

function schemaSelectedBreak(foglio, valore) {
  if (!valore.dataStartBreak) return null;
  return schemaSectionBreaks(foglio).find((voce) => Number(voce.row) === Number(valore.dataStartBreak)) || null;
}

// Scegliere un separatore detta anche il numero: la riga dei prodotti smette
// di essere una cosa da digitare. Sta qui, e non dentro il gestore dell'evento,
// perche' il gestore non e' raggiungibile dalle prove eseguite — e questa e'
// esattamente la parte che si puo' sbagliare.
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

// Il testo del separatore senza i puntini con cui il profilo lo accorcia: e'
// quello che si puo' cercare davvero nella cella, ed e' quello che l'utente
// deve poter accorciare ancora.
function schemaTestoSeparatore(separatore) {
  const testo = String(separatore.text ?? "").trim();
  return testo.endsWith("…") ? testo.slice(0, -1).trim() : testo;
}

// La regola che la pagina rimanda al servizio al posto del numero di riga.
// `offset` si ricava dalle due righe che l'utente vede — il separatore e il
// primo prodotto — quindi non puo' descrivere una riga diversa da quella
// mostrata nell'anteprima.
function schemaDataStartMarker(foglio, valore) {
  const separatore = schemaSelectedBreak(foglio, valore);
  if (!separatore) return undefined;
  // ⚠ Il profilo accorcia a 80 caratteri il testo del separatore, e ci mette i
  // puntini: sul QUERCIA vero cinque separatori su sei sono righe promozionali
  // lunghe. Chiedere al servizio «e' uguale a» un testo mozzato lo farebbe
  // rifiutare, perche' lui confronta con la cella intera.
  const mozzato = String(separatore.text ?? "").trim().endsWith("…");
  const intero = schemaTestoSeparatore(separatore);
  const testo = String(valore.markerText ?? intero).trim();
  if (!testo) return undefined;
  const scarto = Number(valore.dataStartRow) - Number(separatore.row);
  if (!Number.isFinite(scarto) || scarto < 0) return undefined;
  return {
    column: Number(separatore.column),
    // Un testo accorciato — dai puntini del profilo o dall'utente — e' un pezzo
    // di una scritta piu' lunga, e spesso e' il pezzo stabile di una che cambia
    // ogni settimana («PROMO DAL 30/07 AL 27/08»): li' vale «contiene».
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

// ⚠ Due notizie diverse, che la pagina diceva con la stessa frase. «Questo non
// lo conosco» chiede di configurare un fornitore nuovo. «Questo lo conosco e il
// suo listino è cambiato» chiede solo di ricontrollare: le tendine sono già
// compilate con quello che il registro dichiara. Misurato il 21 agosto 2026 su
// «3listino_Cipresso.xlsx»: riconosciuto come cipresso_v1 con confidenza 0,98,
// tutte le colonne al loro posto, e cambiati soltanto il nome del foglio — che
// porta la data, quindi cambia ogni settimana — e la riga delle intestazioni.
function renderPercheEQui(documento) {
  const motivo = documento.reason || {};
  if (String(motivo.state || "") === "RUOLO_SBAGLIATO") {
    const chi = String(motivo.supplierName || "").trim();
    return `<p>${escapeHtml(chi
      ? `Questo documento lo riconosco: è il listino di ${chi}, ed è stato caricato con il tipo sbagliato. Cambia «Tipo di documento» qui sotto.`
      : "Questo documento lo riconosco, ma è stato caricato con il tipo sbagliato. Cambia «Tipo di documento» qui sotto.")}</p>`;
  }
  // ⚠ «Questo non lo conosco» era anche la frase di un documento a cui manca
  // UNA intestazione su cinque, con tutte le altre al posto giusto. Il 21
  // agosto 2026 è costato un adattatore imparato sopra quello spedito di
  // BETULLA: il listino era stato aperto in Excel e risalvato con la cella C1
  // svuotata, e chi ha letto «non riconosco» ha configurato un fornitore
  // nuovo. Le parole le decide il registro, che è l'unico a sapere quale
  // cella manca e dove stava.
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
  // Le colonne riviste a mano su un listino che il programma riconosce: qui non
  // c'è niente che non vada, e dirlo serve — chi apre questa schermata deve
  // sapere che sta guardando il listino giusto prima di cambiarne le colonne.
  if (String(motivo.state || "") === "NOTO") {
    const chi = String(motivo.supplierName || "").trim();
    return `<p>${escapeHtml(chi
      ? `Questo listino è di ${chi} e lo riconosco: le colonne qui sotto sono quelle che uso oggi.`
      : "Questo documento lo riconosco: le colonne qui sotto sono quelle che uso oggi.")}</p>`;
  }
  if (String(motivo.state || "") !== "VARIATO") return "";
  const fornitore = String(motivo.supplierName || "").trim();
  const cambiato = asArray(motivo.changed).map((voce) => String(voce || "")).filter(Boolean);
  // Se il programma non ha proposto nessun fornitore, le tendine sono vuote:
  // «le colonne qui sotto sono già quelle che usavo» sarebbe falso. Succede sui
  // fornitori che si riconoscono dalla forma delle colonne — LARICE, OFFERTE —
  // che non hanno intestazioni da proporre.
  const compilate = Boolean(schemaDocumentValue(documento).supplierChoice);
  const chi = fornitore ? `Questo listino è di ${fornitore} e lo conosco` : "Questo listino lo conosco";
  // ⚠ «Quello che è cambiato:» e non «è cambiato X»: le voci sono di genere e
  // numero diversi — «la riga delle intestazioni», «il nome del foglio», «una
  // colonna dichiarata non c'è più» — e qualunque verbo concordato ne sbaglia
  // almeno una.
  const cosa = cambiato.length ? cambiato.join(", ") : "qualcosa nella sua disposizione";
  const coda = compilate
    ? "Le colonne qui sotto sono già quelle che usavo: controllale e conferma."
    : "Le colonne di questo fornitore non si leggono dalle intestazioni, quindi qui sotto vanno indicate.";
  return `<p>${escapeHtml(`${chi}. Quello che è cambiato: ${cosa}. ${coda}`)}</p>`;
}

// Lo scontro con un listino già caricato, detto **mentre si sceglie** e non
// dopo la conferma. La regola è quella della catena
// (`_piu_recente_per_ruolo`): entra il file copiato per ultimo nella cartella
// dei caricamenti. A parità di data non si indovina: si dice che non si sa.
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
        ${/* ⚠ Fuori dal ramo dei soli fornitori: `schemaOccupatoDa` ha una riga
             dedicata al gestionale — «l'elenco del gestionale» — e
             `ruoli_gia_occupati` la produce apposta, ma finché la chiamata
             stava là dentro quel ramo era codice morto e il gestionale vero
             usciva dal confronto senza una parola. */ ""}
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

// La colonna «Disponibilità» da sola non dice niente. Chi legge il listino
// parte da «disponibile» e guarda la cella solo se sa QUALI valori significano
// disponibile: senza quell'elenco la colonna scelta non ha nessun effetto, e le
// righe con NO restano ordinabili e possono vincere il confronto (6 settembre
// 2026). L'elenco lo scrive l'utente, che il suo listino ce l'ha davanti:
// inventarlo nel codice — «SI», «S», «X», «DISP» — sarebbe la convenzione di un
// fornitore cablata addosso a tutti gli altri.
//
// Il campo compare solo con la colonna scelta, come «come sono scritte» qui
// sotto: senza colonna non c'è niente da dichiarare.
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

// Le condizioni commerciali sono una colonna come le altre — «dove scrive le
// sue offerte» — e per questo stanno dentro «Altre colonne» e non in un
// riquadro nuovo: è lo stesso gesto delle righe qui sopra.
//
// ⚠ Il buco misurato su QUERCIA il 17 agosto 2026: un fornitore nuovo si legge e
// si compila dalla pagina, ma le sue offerte no, perché `commercial_conditions`
// si scriveva a mano nel registro e ce l'aveva solo LARICE.
//
// Le altre due domande compaiono solo quando servono davvero. La forma si
// chiede dopo che la colonna c'è (senza colonna non c'è niente da leggere), e
// le due colonne in fondo solo per «un blocco di righe», che è l'unica forma
// che le pretende — il servizio rifiuta la dichiarazione senza, e chiederle
// sempre vorrebbe dire mostrarne quattro dove ne basta una.
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

// Il titolo dice quale delle due cose sta succedendo, e quando succedono
// tutt'e due le dice tutt'e due. «Non riconosco le colonne di 2 documenti» su un
// file nuovo più il listino di CIPRESSO era falso per metà.
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

// Lo stesso selettore, aperto dalla scheda di un listino invece che da una
// catena ferma. Cambiano il titolo — qui non c'è niente che non si riconosca —
// e i comandi: si salva e basta, il confronto lo rifà l'utente quando vuole.
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
    ${/* Niente intestazione qui: il nome del file è già nel titolo della
         pagina e in cima alla scheda del documento, e lo stato («da
         controllare» / «controllato») lo dichiara la scheda stessa. */ ""}
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
  // ⚠ L'errore NON porta più via il modulo. Fino al 21 agosto 2026 un «Prova le
  // colonne» non superato faceva sparire tutte le tendine e lasciava un
  // riquadro rosso: i valori restavano in memoria, ma non c'era più nessun campo
  // da cambiare che potesse azzerare l'errore. L'unica uscita era ricaricare la
  // pagina e ricominciare da capo. Adesso l'errore sta accanto al pulsante che
  // l'ha prodotto, e il lavoro resta dov'è.
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

// «in corso da 2 minuti», detto in italiano e senza secondi: chi guarda una
// barra ferma vuole sapere se e' un'attesa normale, non cronometrarla. Sotto il
// minuto non si dice niente: a quel punto la barra si muove ancora da sola.
function daQuantoGira(iniziatoIl) {
  const inizio = Date.parse(String(iniziatoIl || ""));
  if (!Number.isFinite(inizio)) return "";
  const minuti = Math.floor((Date.now() - inizio) / 60000);
  // Sotto il minuto non si dice niente: fin lì la barra si muove ancora da
  // sola. Sopra non c'è nessun tetto, ed è voluto: «in corso da 47 minuti» è
  // esattamente la frase che serve quando qualcosa non va.
  if (minuti < 1) return "";
  return `In corso da ${contati(minuti, "minuto", "minuti")}.`;
}

function renderAvanzamentoPipeline() {
  const stato = state.pipeline.stato;
  if (!stato || !state.pipeline.chiesto) return "";
  // ⚠ La barra è per un ricalcolo che esiste. IN_ATTESA non ne ha uno — né
  // quando non è mai partito niente, né quando i documenti sono cambiati dopo
  // l'ultimo confronto — e una barra a zero con nove fasi «in attesa» sarebbe
  // rumore. Quello che c'è da dire in quel caso lo dice la fascia di
  // renderCambiamentoDocumenti(), che non dipende da `chiesto`.
  if (String(stato.stato || "") === "IN_ATTESA") return "";
  const fasi = Array.isArray(stato.fasi) ? stato.fasi : [];
  const percento = Number(stato.avanzamento?.percento || 0);
  const fermata = stato.fermata;
  const richiedeMappatura = schemaMappingRequired(stato);
  const numeri = renderNumeriPipeline(stato.numeri || {});
  // ⚠ «Confronto aggiornato: 457 prodotti, 4 fornitori.» è l'avviso che dice
  // che il programma ha funzionato, e i suoi numeri sono già nella riga qui
  // sotto. Quando la catena è finita bene e i numeri ci sono, il messaggio non
  // si scrive. Resta dov'è utile: mentre gira, quando si è fermata, e quando i
  // numeri mancano — lì è l'unica cosa che dice come è andata.
  const messaggio = String(stato.messaggio || "");
  const diciIlMessaggio = Boolean(messaggio) && !richiedeMappatura
    && !(String(stato.stato || "") === "COMPLETATO" && numeri && !fermata);
  // Gli avvisi della catena arrivano anche dentro il confronto, e il riquadro
  // in cima alla pagina li mostra già: stampati anche qui erano lo stesso
  // avviso due volte nella stessa schermata (15 agosto 2026).
  // ⚠ …ma solo quelli che quel riquadro sta DAVVERO stampando. Mentre la
  // mappatura guidata è aperta `renderSourceWarnings` tiene su solo i
  // bloccanti, e togliere di qui anche gli altri li faceva sparire da tutte e
  // due le parti: il riquadro in cima non li mostrava più, e questo li toglieva
  // perché «li mostra già il riquadro in cima».
  const gia = new Set(avvisiVisibiliInCima().map((avviso) => avviso.code).filter(Boolean));
  const avvisi = asArray(stato.avvisi)
    .map((avviso) => normalizeIssue(avviso, `pipeline-${avviso?.code || ""}`))
    .filter((avviso) => !avviso.code || !gia.has(avviso.code));
  // ⚠ A ricalcolo finito la barra resta piena da un capo all'altro, e una
  // barra piena è l'avviso «ha funzionato» disegnato invece che scritto. Serve
  // mentre la catena gira, e quando si è fermata — lì dice a che punto era.
  const barra = String(stato.stato || "") === "COMPLETATO" && !fermata
    ? ""
    : `<div class="pipeline-progress__bar"><div class="pipeline-progress__fill" style="width: ${percento}%"></div></div>`;
  // Da quanto sta girando. La sesta fase costa due minuti per dichiarazione del
  // codice stesso: la barra ci arriva al 55,6 % e poi resta immobile per il
  // tempo più lungo dell'intero ricalcolo. Una barra ferma senza una parola si
  // legge come «si è piantato», e la reazione naturale è chiudere il programma
  // proprio mentre la catena lavora.
  // ⚠ Il tempo si conta dal timbro del servizio, `iniziatoIl`, non da un
  // `Date.now()` fissato in pagina: altrimenti un ricaricamento del browser a
  // metà ricalcolo farebbe ripartire il contatore da zero e direbbe il falso.
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
      ${/* ⚠ Qui c'era un renderAlert «Un listino ha colonne che non conosco /
           Indicamele qui sotto e riparto»: il doppione esatto del titolo della
           procedura guidata, che sta due centimetri sotto e adesso dice anche
           QUALE dei due casi è. Due scatole per la stessa notizia. */ ""}
      ${!richiedeMappatura && fermata ? renderAlert({
        title: "Il confronto si è fermato",
        // La frase «se qualcosa non torna, il confronto di prima resta al suo
        // posto» stava sotto il pulsante, sempre, anche quando non era
        // successo niente. Serve qui: è il momento in cui uno si chiede se ha
        // perso il lavoro.
        message: `${String(fermata.message || "")}${confrontoDisponibile() ? " Il confronto di prima è ancora al suo posto." : ""}`,
        severity: "error",
        blocking: true,
      }) : ""}
      ${/* ⚠ Gli avvisi della catena venivano stampati uno per uno, e stavano in
           fondo, fra l'ultima tendina e «Continua». Dieci avvisi con lo stesso
           titolo diventavano dieci riquadri se arrivavano di qui e un gruppo
           solo se arrivavano dal confronto: sono le stesse dieci frasi.
           `renderAvvisi` è il raggruppamento che il resto del programma usa da
           sempre. */ ""}
      ${renderAvvisi(avvisi, "catena")}
      ${richiedeMappatura ? renderSchemaMappingWizard() : ""}
    </div>`;
}

function renderPipelinePanel() {
  const situazione = statoPaginaImporta();
  const inCorso = situazione === "RICALCOLO_IN_CORSO";
  const richiedeMappatura = schemaMappingRequired();
  const comando = comandoConfronto(situazione);
  // ⚠ Il titolo era «Rifai il confronto», cioè la stessa frase del pulsante
  // qui sotto: sembravano due comandi. Il titolo dice di che cosa parla il
  // riquadro, il pulsante dice che cosa fa.
  //
  // La spiegazione serve a chi non ha ancora un confronto: dice che cosa
  // succede premendo e quanto dura. A confronto fatto la stessa frase è la
  // didascalia di un pulsante che si preme ogni settimana, e la promessa «gli
  // originali non si toccano» è già scritta sotto il titolo della pagina.
  const spiegazione = confrontoDisponibile()
    ? ""
    : "Legge i documenti caricati, li confronta e prepara le offerte: ci vogliono alcuni minuti. Nessun ordine viene inviato.";
  // Il comando sta nell'intestazione del riquadro, dove sta l'azione di quel
  // riquadro: da solo in fondo lasciava in mezzo una fascia vuota, e sembrava
  // il pulsante della pagina invece che del ricalcolo.
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

// «Inizia nuova comparazione» — chiesto da Daniele il 22 agosto 2026.
// Il giro di ogni lunedì era togliere l'elenco del gestionale e poi i listini
// uno per uno; con cinque fornitori sono sei cancellazioni prima di poter
// caricare i nuovi. E chi saltava il giro non se ne accorgeva subito: due
// listini dello stesso fornitore ne fanno entrare in confronto **uno solo**,
// scelto sulla data di modifica del file.
//
// Sta in alto, accanto a «Impostazioni», e non fra i comandi di caricamento:
// non è un modo di caricare un documento, è il gesto con cui si apre la
// settimana.
function renderComandoNuovaComparazione(bloccato) {
  // Niente da svuotare: nessun documento e nessun confronto. Un comando che
  // non farebbe niente si spegne invece di rispondere «fatto» al vuoto.
  const c_e_qualcosa = state.review.files.length > 0 || state.review.products.length > 0;
  const spento = bloccato || !c_e_qualcosa || state.nuovaComparazione.inCorso || state.nuovaComparazione.chiedendo;
  return `<button class="button button--ghost button--small" type="button" data-action="nuova-comparazione" ${spento ? "disabled" : ""}>${
    state.nuovaComparazione.inCorso ? "Attendere…" : "Inizia nuova comparazione"
  }</button>`;
}

// La conferma è **una riga**, non una finestra: quello che sparisce sono copie
// — i documenti in ingresso sono di sola lettura e il programma se ne fa una
// sua (regola 1) — quindi il gesto non merita il peso di una domanda a tutto
// schermo. Ma le due cose che la riga dice servono tutte e due: la prima
// perché il comando cancella davvero qualcosa, la seconda perché la paura di
// chi legge non è quello che il comando toglie, è quello che **non sa** se
// toglie. Conferme e ordini in attesa restano, e va detto qui.
function renderConfermaNuovaComparazione() {
  if (!state.nuovaComparazione.chiedendo) return "";
  // ⚠ Non si filtra per `role`: `normalizeReview` non lo copia — la pagina
  // riconosce l'elenco del gestionale dalle parole di `kind`/`supplier`
  // (`isManagementFile`), non da un ruolo. Filtrando per ruolo il conto usciva
  // **zero** con quattro documenti in pagina. `files` è già esattamente
  // l'elenco delle schede che si vedono qui sopra, ed è quello il numero che
  // chi legge può verificare a occhio.
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
  // Rivedere le colonne di un listino è un lavoro a sé: prende la pagina, e si
  // esce da dove si è entrati. Mescolarlo all'elenco dei documenti vorrebbe
  // dire una tabella di anteprima in mezzo alle schede dei file.
  if (colonneAperteAMano()) return renderColonneDocumento();
  const blockingFiles = state.review.files.filter((file) => fileStatus(file).tone === "danger").length;
  const managementFiles = state.review.files.filter(isManagementFile);
  const supplierFiles = state.review.files.filter((file) => !isManagementFile(file));
  const situazione = statoPaginaImporta();
  const continua = comandoContinua(situazione);
  // Durante il ricalcolo i comandi di caricamento non restano attivi come se
  // niente fosse: cambierebbero i documenti sotto il confronto che sta girando.
  const bloccato = situazione === "RICALCOLO_IN_CORSO";
  return `
    ${pageHeading(
      "1. Importa i dati",
      "Carica prima l’elenco dei prodotti da ordinare, poi i listini dei fornitori. I documenti originali non vengono modificati.",
      `${renderComandoNuovaComparazione(bloccato)}<button class="button button--ghost button--small" type="button" data-action="apri-impostazioni">Impostazioni</button>`,
    )}
    ${renderConfermaNuovaComparazione()}
    ${/* La domanda sugli ordini della settimana scorsa sta anche qui, e non
         solo in pagina 2: il momento in cui ci si chiede «è arrivata?» è
         quello in cui si comincia la comparazione nuova. La condizione è che
         non ci sia un confronto da guardare — subito dopo il comando, o al
         primo avvio — perché a confronto pieno la pagina 1 è quella dei
         documenti e la domanda resta dov'era. */ ""}
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

// L'ordine alfabetico è quello italiano, con i numeri contati come numeri:
// "NEVAL 10ML" prima di "NEVAL 100ML". `sensitivity: "base"` mette accenti e
// maiuscole dove uno se li aspetta invece che in fondo all'elenco.
const CONFRONTO_ALFABETICO = { sensitivity: "base", numeric: true };

function sortedProducts(products) {
  const verso = state.filters.sort === "nome-desc" ? -1 : 1;
  if (state.filters.sort !== "nome" && state.filters.sort !== "nome-desc") return products;
  // Copia: `sort` ordina sul posto, e questa lista è la stessa di `state.review`.
  return products.slice().sort((a, b) => {
    const nome = String(a.name || "").localeCompare(String(b.name || ""), "it", CONFRONTO_ALFABETICO);
    // A parità di nome decide il codice a barre: due righe con lo stesso nome
    // sono due articoli diversi, e l'ordine non deve ballare da un ridisegno
    // all'altro.
    if (nome !== 0) return nome * verso;
    return String(a.ean || "").localeCompare(String(b.ean || ""), "it", CONFRONTO_ALFABETICO) * verso;
  });
}

// Le voci di «Mostra», ognuna con la domanda che fa. Un posto solo: lo stesso
// predicato conta le righe nell'etichetta e le filtra nell'elenco, e due
// definizioni della stessa voce si sarebbero separate al primo ritocco.
//
// ⚠ Erano sette e si sovrapponevano. «Da verificare» prendeva qualunque avviso
// — quindi anche i 32 prodotti che nessuno ha, che stavano già in «Senza
// offerta» — e «Senza quantità» era il complemento di «Da ordinare», cioè lo
// stesso elenco letto al contrario. Restano le quattro domande che si fanno
// davvero davanti a cinquecento righe: che cosa ordino, che cosa devo
// confermare, che cosa nessuno ha, che cosa ho messo da parte.
const FILTRI_PRODOTTO = {
  all: { etichetta: "Tutti i prodotti", tieni: () => true, sempre: true },
  ordered: { etichetta: "Da ordinare", tieni: (product) => orderQuantity(product) > 0, sempre: true },
  "to-confirm": { etichetta: "Da confermare", tieni: (product) => orderQuantity(product) > 0 && daConfermare(product) },
  // ⚠ «Nessuno ce l’ha» diceva il falso su una parte dei suoi prodotti.
  // `nessunaOffertaUtilizzabile` risponde alla domanda giusta — «si può
  // ordinare?» — e per quella i rifiutati contano: se hai detto «non è lo
  // stesso articolo» a tutti i fornitori, non c’è più niente da scegliere.
  // Ma il NOME di quell’elenco è un’altra cosa, e su quei prodotti i listini
  // la riga ce l’avevano: a toglierla sei stato tu. Restano due elenchi
  // disgiunti, perché la mossa da fare è diversa: uno si cerca altrove, per
  // l’altro può bastare rivedere una risposta.
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
  // ⚠ Gli espositori NON hanno una voce qui: ce l'hanno già in «Tipo di
  // prodotto», e due strade per lo stesso elenco sono la sovrapposizione che
  // questo filtro è appena stato rifatto per togliere. Gli aggiunti a mano
  // invece non si trovavano da nessuna parte.
  manual: { etichetta: "Aggiunti a mano", tieni: (product) => Boolean(product.addedManually) },
};

// Gli esclusi si vedono soltanto nel loro filtro: sono i prodotti che si è
// deciso di non ordinare, e comparire in mezzo agli altri li rimetterebbe in
// mezzo alle decisioni da prendere.
function passaIlFiltro(product, stato) {
  const voce = FILTRI_PRODOTTO[stato] || FILTRI_PRODOTTO.all;
  if (stato !== "excluded" && isExcluded(product)) return false;
  return voce.tieni(product);
}

// Quanti prodotti mostrerebbe ogni voce. Si contano su tutto l'elenco, non su
// quello già filtrato da ricerca e tipo: un numero che balla mentre si scrive
// nella casella di ricerca non dice più quanto lavoro resta.
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

// Il tipo di prodotto si sceglie soltanto se ce n'è più d'uno da scegliere: sul
// confronto di oggi i «Kit» sono zero e quella voce era una riga da leggere e
// scartare. Con un tipo solo il campo non si disegna affatto.
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

// ⚠ Una voce che non mostrerebbe niente non si disegna: «Esclusi» ha senso
// quando qualcosa è escluso, «Già ordinati» quando c'è un ordine in sospeso, e
// prima di allora sono due righe che si leggono per scoprire che sono vuote.
// «Tutti i prodotti» e «Da ordinare» restano sempre — sono i due modi di
// guardare l'elenco intero — e la voce scelta resta in ogni caso, anche a zero:
// un menù che perde la voce selezionata mentirebbe su che cosa si sta guardando.
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
        ${/* ⚠ «Azzera le quantità predefinite» non diceva che cosa tocca. Il
             codice azzera SOLO i prodotti con `quantitySource === "gestionale"`,
             cioè quelli mai toccati a mano: chi modifica una quantità la fa
             diventare «utente». Ma chi legge ha davanti un pulsante che, per
             quanto ne sa, gli cancella il lavoro di mezz'ora. */ ""}
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

// Che cosa manca alla riga proposta perché la si possa accettare. Le due
// condizioni sono quelle del servizio locale (`available` in
// `build_review_data.offer_from_match`): un prezzo maggiore di zero e il numero
// di pezzi in un collo. Si dice quale delle due manca, perché sono due difetti
// diversi del listino e si correggono in due modi diversi.
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
  // La conferma ha già il suo controllo subito sotto le offerte: ripeterla
  // anche come errore rosso qui sopra aggiunge testo senza aggiungere un'azione.
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
    // ⚠ Il «Sì» c'era sempre, e sul confronto vero non ha mai potuto funzionare:
    // il servizio locale rifiuta una riga senza prezzo e confezione
    // utilizzabili, e rispondeva «Risposta non registrata». Un pulsante che dà
    // errore ogni volta che lo si preme non è una domanda: si dice che cosa
    // manca, e il «No» resta — quello si può sempre dare.
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

// ⚠ Un «No» alla proposta del fornitore era irreversibile. L'avviso spariva per
// entrambe le risposte, e la decisione registrata — offer.candidateDecision, che
// il servizio locale conserva insieme alla riga proposta — non veniva letta da
// nessuna funzione di render: chi cliccava «Non è lo stesso» per sbaglio tornava
// esattamente nello stato che quella domanda doveva eliminare, senza rimedio.
// La decisione si vede e si cambia, per tutta la durata di questo confronto.
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
        // Tornare al «Sì» ha senso solo se la riga proposta ha ancora prezzo e
        // confezione utilizzabili: senza, il servizio locale rifiuterebbe la
        // risposta e il pulsante sarebbe una promessa vuota.
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

// --- Ordini non ancora ricevuti, in cima alla pagina 2 ---------------------
// Domanda esplicita con due pulsanti grandi: niente window.confirm. Se non c'è
// nulla in sospeso non viene disegnato nulla, nemmeno un riquadro vuoto.

// Un "non ancora arrivata" non archivia la domanda: la rimanda. Il momento in
// cui torna lo dice il servizio locale (askAgainAt, una settimana dopo la
// risposta), perché è lui a tenere lo storico; qui si guarda solo l'orologio.
// Un ordine senza quella data non è mai stato risposto e la domanda è dovuta.
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
  // L'unità è quella con cui si era ordinato allora, non quella di oggi: lo stesso
  // codice può essere un espositore una settimana e un prodotto normale quella dopo.
  const label = String(entry.unit || product.orderUnitLabel || "colli");
  const unit = quantity === 1 ? orderUnitSingular(label) : label;
  const supplier = pendingOrderSupplierLabel(entry);
  const day = formatDayMonth(entry.orderedAt);
  const quantityText = quantity > 0 ? `${formatInteger(quantity)} ${unit} da ${supplier}` : `merce da ${supplier}`;
  const closing = quantity === 1 ? "non ancora ricevuto" : "non ancora ricevuti";
  // Un codice a barre condiviso da più articoli: attribuire la quantità a
  // QUESTO articolo regalava «20 colli già ordinati» anche a chi ne aveva
  // zero (revisione avversariale R4). La quantità è dell'ordine sul codice,
  // non dell'articolo, e la frase lo dice.
  if (entry.sharedWith > 1) {
    const base = day ? `${quantityText} il ${day}, ${closing}` : `${quantityText}, ${closing}`;
    return `Già ordinato sullo stesso codice a barre: ${base}. Il codice è condiviso da ${formatInteger(entry.sharedWith)} articoli del confronto: la quantità non è attribuibile a questo soltanto.`;
  }
  return day
    ? `Già ordinato: ${quantityText} il ${day}, ${closing}.`
    : `Già ordinato: ${quantityText}, ${closing}.`;
}

// Avviso informativo: non blocca la compilazione e non cambia nulla dell'ordine.
function renderPendingOrderNotice(product) {
  const entries = pendingOrdersFor(product);
  if (!entries.length) return "";
  return `
    <div class="pending-order-notice">
      ${entries.map((entry) => `<span>${escapeHtml(pendingOrderNoticeText(product, entry))}</span>`).join("")}
    </div>`;
}

// --- Pannello offerte in cima alla pagina 2 -------------------------------
// Puramente informativo: non tocca prezzi, totali né fornitore selezionato.
// Riusa promotionText()/renderBadge() già usati da renderPromotionSummary()
// (pagina 3) invece di duplicarne la logica di formattazione.

const PROMOTION_HIGHLIGHT_STATUSES = ["vicina", "ottenuta"];
const PROMOTION_HIGHLIGHT_LIMIT = 6;
const PROMOTION_STATUS_ORDER = { vicina: 0, ottenuta: 1, non_raggiunta: 2, da_verificare: 3 };

// --- Fase 8: da elenco a decisione ----------------------------------------
// Su questi listini le condizioni riconosciute sono 188, ma 175 sono sconti già
// compresi nel prezzo al pezzo su cui si sceglie il fornitore: non sono offerte
// da leggere, sono il prezzo. Mostrarle insieme alle poche che cambiano una
// decisione è il motivo per cui il pannello risultava ingestibile.

function promotionIsAlreadyInPrice(promotion) {
  return Boolean(promotion?.economic_effect?.already_applied);
}

// Azionabile: non è già nel prezzo E si sa a quali prodotti si applica. Una
// condizione senza prodotti abbinati non dice quanto manca né a cosa serve:
// resta leggibile, ma non va contata fra quelle che cambiano l'ordine.
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

// Prima quelle a cui manca meno: è l'ordine in cui servono, perché una soglia
// lontanissima non cambia nessuna decisione di oggi.
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

// La riga di conteggio deve dire la verità: quante ne sono state lette e quante
// possono davvero cambiare questo ordine. "LARICE 185" da solo comunica un
// carico di lavoro che non esiste.
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
  // Sotto una certa quantità l'elenco si legge: il campo comparirebbe solo per
  // farsi guardare.
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
            ${/* ⚠ La riga «459 prodotti trovati (451 dall'elenco, 6 espositori,
                 2 aggiunti a mano)» diceva QUANTI ma non QUALI: i due aggiunti
                 a mano non si distinguevano da nessuna parte, e per ritrovarli
                 bisognava ricordarsi che cosa si era aggiunto. Un'etichetta
                 sulla scheda e una voce nel filtro «Mostra», e solo quando ce
                 n'è almeno uno. */ ""}
            ${product.addedManually ? renderBadge("Aggiunto a mano", "info") : ""}
            ${excluded ? renderBadge("Escluso", "neutral") : ""}
            ${/* Il rosso e l'ambra erano una striscia di 4 px sul fianco della
                 scheda, e fra loro correvano 1,33:1: per chiunque abbia una
                 carenza sul rosso-verde, o su un monitor da negozio, erano la
                 stessa striscia. Scorrendo venti schede non si trovava quella
                 che ferma il lavoro. La riga dei metadati contava già i
                 controlli, ma «3 controlli» si legge uguale che uno dei tre
                 blocchi la compilazione o no: la distinzione va detta, non
                 colorata. Un prodotto escluso non ha controlli — collectIssues
                 salta chi ha quantità zero — quindi questa etichetta e
                 «Escluso» non compaiono mai insieme. */ ""}
            ${blocker ? renderBadge("Da sistemare", "danger") : issues.length ? renderBadge("Da controllare", "warning") : ""}
          </div>
          <div class="product-main__meta">
            <span>${product.ean ? `EAN ${escapeHtml(product.ean)}` : "Senza EAN padre"}</span>
            ${/* Diceva «3 offerte», e «offerta» in un negozio vuol dire sconto:
                 chi legge conta gli sconti, non i fornitori che hanno il
                 prodotto. Si contano i fornitori distinti, non le righe, cosi'
                 il numero resta vero anche se un listino porta due volte lo
                 stesso articolo. */ ""}
            <span>${escapeHtml(contati(new Set(product.offers.filter((candidate) => candidate.available).map((candidate) => candidate.supplierId)).size, "fornitore ce l’ha", "fornitori ce l’hanno"))}</span>
            ${/* ⚠ «0 fornitori ce l'hanno» su un prodotto rifiutato a tutti è la
                 stessa bugia che il riquadro delle offerte ha già smesso di
                 dire: quei fornitori la riga ce l'hanno, sei stato tu a dire che
                 è un altro articolo. Il conteggio dei disponibili resta quello
                 che è — serve a sapere chi si può ordinare — e accanto si dice
                 chi manca perché l'hai escluso tu. */ ""}
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
          ${/* Il titolo «Confronto dei fornitori» stava su ogni scheda, venti
               volte per pagina, sopra una tabella la cui prima colonna si
               chiama «Fornitore». */ ""}
          ${renderProductIssues(product)}
          ${renderCandidateDecisions(product)}
          ${renderOfferGrid(product)}
          ${renderConfirmation(product)}
          ${renderComponents(product)}
        </div>
      `}
    </article>`;
}

// Da dove vengono i prodotti che si stanno guardando. ⚠ «Continua a dirmi 459
// prodotti» (15 agosto 2026): il numero era giusto — 451 righe dell'elenco, 6
// espositori, 2 aggiunti a mano — ma da solo somigliava al totale di un elenco
// vecchio, e nessuna riga della pagina lo scomponeva.
function composizioneDellElenco() {
  const prodotti = state.review.products;
  const aggiunti = prodotti.filter((prodotto) => prodotto.addedManually).length;
  const espositori = prodotti.filter((prodotto) => prodotto.itemType === "display").length;
  const dallElenco = prodotti.length - aggiunti - espositori;
  const pezzi = [`${formatInteger(dallElenco)} dall’elenco del gestionale`];
  // ⚠ Dove si trovano, non solo quanti sono: «6 espositori dei listini, 2
  // aggiunti a mano» diceva che c'erano e lasciava cercarli a memoria.
  if (espositori) {
    pezzi.push(`${formatInteger(espositori)} ${espositori === 1 ? "espositore dei listini" : "espositori dei listini"} (filtro «Tipo di prodotto»)`);
  }
  if (aggiunti) {
    pezzi.push(`${formatInteger(aggiunti)} ${aggiunti === 1 ? "aggiunto" : "aggiunti"} a mano (filtro «Mostra»)`);
  }
  return pezzi.join(", ");
}

// Un prodotto escluso non compare in nessun filtro tranne «Esclusi»: se lo si
// cerca per nome, la pagina risponde «Nessun prodotto trovato» e chi cerca
// conclude che nell'elenco non c'è. ⚠ È successo il 15 agosto 2026 con
// KALIDERMA, che era nell'elenco, aveva la sua quantità, ed era escluso.
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
    ${/* La frase sull'ordine dei fornitori sta qui, una volta per pagina, e non
         sotto ogni griglia: il titolo «Confronto dei fornitori» era su ogni
         scheda, venti volte per pagina, ed è stato tolto per questo. */ ""}
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

// --- Il visualizzatore dei listini ------------------------------------------
//
// A che cosa serve, con il caso da cui è nato. `LUXA SAPONE LIQ. EROG.250ML` sta
// nel gestionale col codice 4009428623194, che solo CIPRESSO usa (1,28 €/pz); lo
// stesso articolo sta su NOCE, LARICE e BETULLA sotto 8729721830575, a 1,15,
// 1,1625 e 1,19. Nessun punteggio può dedurlo — nel nome del gestionale la
// variante (`ORIGINAL` contro `SETA`) non c'è affatto, `EROG.` sta per
// erogatore — mentre una persona col listino davanti la vede in un secondo.
//
// Mostra il listino **come il programma l'ha letto**, non il foglio Excel: se
// una colonna è letta storta, qui si vede storta, ed è l'informazione che serve.
// Le righe che il programma ha scartato si contano sempre, anche quando la
// pagina non ne mostra nessuna.

function apriIlListino(prodottoId, fornitore = "", riga = null) {
  ricordaChiApreLaFinestra();
  state.listino = {
    ...state.listino,
    aperto: true,
    prodottoId: String(prodottoId || ""),
    fornitore: String(fornitore || ""),
    // ⚠ `fornitori` resta fuori dall'azzeramento, ed è voluto: un elenco già
    // visto sopravvive anche se la chiamata nuova non arriva affatto (servizio
    // spento, rete caduta), e la tendina resta usabile.
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

// --- In quale colonna si scrive l'ordine ----------------------------------
// La domanda si può fare in qualunque momento, e la risposta vale da subito:
// il servizio locale rifà la configurazione di scrittura appena l'ha accettata.

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
  // Una volta sola, adesso: le colonne arrivano fra un attimo e la tendina non
  // c'è ancora, quindi il fuoco cade sul «×» della finestra. Spostarlo di nuovo
  // quando la risposta arriva lo toglierebbe di sotto le dita a chi nel
  // frattempo ha già tabulato.
  portaIlFuocoDentro(["#colonna-ordine-scelta", ".colonna-dialog .dialog-close"]);
  const richiesto = String(fornitore || "");
  try {
    const payload = await requestJson(`${API.colonnaOrdine}?fornitore=${encodeURIComponent(fornitore)}`);
    // ⚠ Non basta che una finestra sia aperta: dev'essere ancora la SUA. Si
    // apre «Cambia colonna» su BETULLA, si chiude prima della risposta e si apre
    // quella di LARICE: la risposta di BETULLA arrivava dopo e metteva le sue
    // colonne dentro uno stato che dichiara LARICE. Da li' «Scrivi l'ordine
    // qui» manderebbe al servizio LARICE con il numero di una colonna scelta
    // sul documento di BETULLA.
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
      // Si parte da quella in uso: la finestra si apre su dove si è, non su
      // una scelta già cambiata che basterebbe confermare per sbaglio.
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
    // La finestra si chiude scrivendo lo stato, non passando da
    // `chiudiLaColonnaDOrdine()`: il fuoco va restituito qui, altrimenti chi
    // salva con la tastiera resta senza segno.
    restituisciIlFuoco();
    // Una riuscita a metà non si racconta come una riuscita: la colonna è
    // cambiata, ma se la configurazione di scrittura non si è rifatta va detto
    // con il tono di un avviso, non con quello di un successo.
    showToast(esito?.message || "Colonna cambiata.", esito?.avviso ? "error" : "success");
    // Le colonne del documento le tiene la pagina per tutta la durata della
    // run: senza questo la scheda continuerebbe a dire la colonna di prima.
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
  // ⚠ Chi arriva ultimo non ha ragione. Le letture partono anche in fretta —
  // doppio clic su «Righe successive», o una parola scritta nella ricerca
  // mentre la prima lettura e' in volo — e senza un numero di richiesta la
  // pagina applicava QUALUNQUE risposta arrivasse: la prima poteva finire dopo
  // la seconda e rimettere in tabella le righe di una ricerca che non si vede
  // piu'. Da quella tabella si preme «E' questo», che scrive un abbinamento
  // permanente in `conferme.db`: e' il posto del programma in cui una riga
  // sbagliata costa di piu'.
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
      // Il nome viene dal registro, che il servizio ha già letto: quando il
      // listino chiesto non è sfogliabile non c'è nessuna voce in `fornitori`
      // da cui prenderlo, e nel titolo finiva l'identificativo.
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

// L'abbinamento fatto a mano: l'offerta entra subito nel confronto, e i due
// codici a barre restano dichiarati uguali per i ricalcoli futuri. La frase che
// torna dal servizio dice quale dei due effetti è avvenuto: quando il prodotto
// o la riga non hanno un codice, il secondo non è possibile, e dirlo è l'unico
// modo di non promettere una cosa che salta al primo ricalcolo.
async function abbinaLaRiga(riga) {
  if (mode === "demo" || state.listino.abbinando) return;
  state.listino.abbinando = String(riga);
  state.listino.esito = "";
  rerenderPreservingFocus();
  try {
    // ⚠ PRIMA il salvataggio, poi la richiesta — la stessa ragione di
    // `addCatalogProduct` e delle due rotte sorelle: qui sotto `loadReview()`
    // rilegge il confronto dal servizio e SOSTITUISCE quello in pagina, e una
    // quantità scritta meno di 450 ms fa — o rimasta indietro perché un
    // salvataggio è fallito e sta per riprovare — non è ancora sul disco. La
    // rilettura se la porta via e il salvataggio dopo consolida il numero
    // vecchio, cioè un ordine sbagliato, senza che niente lo dica (6 settembre
    // 2026). Con lo stato pulito `saveState()` torna vero subito.
    const saved = await saveState();
    if (!saved) throw new Error("Le modifiche correnti non sono ancora state salvate.");
    const esito = await requestJson(API.matchAbbina, {
      method: "POST",
      body: JSON.stringify({
        // ⚠ La run, come la mandano gia' il rifiuto e la risposta alla
        // proposta. `productId` e `sourceRow` sono posizionali — la riga del
        // gestionale e la riga del listino — quindi su un confronto rifatto
        // indicano altri due articoli: senza dire su QUALE confronto e' stato
        // premuto «È questo», il servizio abbina quei due e si ricorda per
        // sempre che i loro codici sono lo stesso articolo. Basta una seconda
        // scheda ferma al confronto di prima (6 settembre 2026).
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

// --- «Questi due codici sono lo stesso articolo» --------------------------
// Stavano in fondo alla finestra «Sfoglia i listini», cioè dentro un prodotto:
// per rileggerle bisognava aprirne uno qualunque, e l'elenco mostrava due
// numeri di tredici cifre — senza i nomi, che sono l'unica cosa con cui si
// giudica se la dichiarazione è giusta. Adesso stanno in Impostazioni, con i
// nomi, il fornitore e una ricerca: una dichiarazione sbagliata entra in ogni
// confronto futuro e su tutti i fornitori, quindi deve essere rileggibile
// senza avere un confronto aperto davanti.

async function caricaLeUguaglianze({ query = null } = {}) {
  if (mode === "demo") return;
  const magazzino = state.impostazioni.uguaglianze;
  if (query !== null) magazzino.query = String(query);
  magazzino.caricando = true;
  // ⚠ Conservando il fuoco: si cerca mentre si scrive, e un `render()` secco
  // toglierebbe il cursore dal campo di ricerca a metà parola.
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
  // La stessa attesa della ricerca nel listino: si cerca mentre si scrive, ma
  // non a ogni tasto.
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
    // L'elenco aggiornato torna con la risposta: rileggerlo sarebbe un giro a
    // vuoto. ⚠ Ma torna **intero**, senza la ricerca in corso applicata, quindi
    // se una ricerca c'è si richiede — altrimenti l'elenco si allargherebbe da
    // solo dopo una cancellazione.
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

// Le conferme date — «sì, questa riga di listino è il mio prodotto» — non si
// rileggono da nessuna parte: il magazzino è un file SQLite, e `app/data/` è
// fuori da git, quindi non ne esiste nessuna copia. Il collegamento qui sotto
// è l'unico modo di farsene una senza copiare a mano un file aperto.
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

// Una dichiarazione per intero: che cosa chiede il gestionale, che cosa dà il
// fornitore, e quando è stata fatta. ⚠ I nomi sono quelli **normalizzati** —
// maiuscoli, senza punteggiatura — perché sono quelli con cui il programma
// confronta: mostrarne di più belli vorrebbe dire mostrare qualcosa di diverso
// da quello su cui la dichiarazione vale.
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

// Che cosa c'è dentro una colonna, in una riga sola. È l'informazione che
// decide: il programma rifiuta soltanto quello che sa dimostrare (una colonna
// che legge lui, una colonna di formule), il resto lo deve poter valutare chi
// il listino ce l'ha davanti. Misurato il 18 agosto 2026: la colonna d'ordine
// di LARICE contiene 641 titoli di sezione, quindi «la colonna dev'essere
// vuota» sarebbe una regola che rifiuta la situazione di oggi.
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
  // Una colonna con dentro qualcosa non si rifiuta — si dice. Chi sceglie la
  // colonna del suo fornitore sa che cosa c'è dentro; chi non lo sa, adesso lo
  // legge prima di premere.
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
          ${/* In errore questa finestra non aveva nessun comando utile: due
               «chiudi» e un «salva» disabilitato, e per riprovare bisognava
               chiuderla e ritrovare il pulsante del fornitore. */ ""}
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
          ${/* ⚠ Quando il fornitore chiesto non è sfogliabile non ha nessuna
               voce nell'elenco, e senza la riga disabilitata qui sotto il
               `<select>` mostrerebbe il primo della lista mentre lo stato dice
               un altro: sceglierlo dalla tendina non farebbe scattare nessun
               `change`, e la finestra sembrerebbe bloccata. */ ""}
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
  // Un listino che non si è potuto sfogliare non ha righe da contare, e
  // «0 righe · tutte ordinabili» sotto l'avviso che spiega perché sarebbe
  // rumore che contraddice.
  if (listino.problema) return "";
  const cercate = listino.query.trim()
    ? `${contati(listino.trovate, "riga trovata", "righe trovate")} su ${formatInteger(listino.totale)}`
    : contati(listino.totale, "riga", "righe");
  // Le righe scartate si dicono sempre: un listino che ne mostra 8.849 su 8.881
  // senza dirlo è un visualizzatore che nasconde.
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

// Il confronto dei fornitori è una tabella, e prima non lo era: ogni fornitore
// aveva il suo riquadro con dentro le stesse quattro etichette («Prezzo per
// collo», «Prezzo per pezzo», «Pezzi per collo», «Totale per la quantità»).
// Su una pagina da venti prodotti sono trecentoventi etichette lette per
// leggere trecentoventi numeri, e i numeri non erano incolonnati — che è
// esattamente il gesto per cui si guarda un confronto.
//
// Le etichette restano nel markup: le nasconde il foglio di stile quando le
// intestazioni in cima bastano, e tornano visibili sugli schermi stretti, dove
// la tabella si impila. Chi legge con la sintesi vocale le sente comunque.
// Come il fornitore chiama quello che sta vendendo, sotto il suo nome.
//
// ⚠ Serve anche — e soprattutto — quando il codice a barre coincide. Parole di
// Daniele, 17 agosto 2026: «su oggetti colorati, anche con EAN esatto, il
// prodotto potrebbe essere di colore diverso, come già visto in passato». Il
// caso vero è sulla scheda `DOPLO PIATTI PIANI 20PZ`: il gestionale non dice il
// colore, BETULLA ha `DOPLO Piatto Riutilizzabile Piano In Polipropilene Bianco`
// con lo stesso EAN, ed CIPRESSO propone `PRIME PIATTI PIANI ROSSI`. La riga
// mostrava soltanto i numeri, e il nome che il fornitore usa — l'unico posto in
// cui il colore è scritto — non compariva da nessuna parte, benché il servizio
// lo mandi da sempre.
//
// L'EAN si scrive solo quando è **diverso** da quello del prodotto: uguale non
// aggiunge niente e sta già in testa alla scheda; diverso vuol dire che quella
// riga è stata agganciata per altra via, ed è la cosa da guardare per prima.
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
  // Di quanto ogni fornitore costa più del più conveniente. ⚠ Il minimo si
  // prende fra le sole offerte DISPONIBILI: su un prodotto dove la più
  // economica non è utilizzabile, la pagina scriverebbe differenze rispetto a
  // un prezzo che nessuno può ordinare. E si dichiara al pezzo, mai sul totale
  // in colli: è la regola scritta due volte in questo file.
  const minimoAlPezzo = disponibili.length
    ? Math.min(...disponibili.map((offer) => finiteNumber(offer.pricePerPiece)))
    : 0;
  // Un fornitore che non ha il prodotto merita una riga, non un riquadro: la
  // frase «non ce l’hanno nel listino di adesso» era ripetuta per
  // ciascuno, e su un prodotto senza offerte occupava tutta la scheda dicendo
  // quattro volte la stessa cosa. Chi porta un motivo suo lo tiene.
  // Le ragioni per cui un'offerta non è utilizzabile sono tre, e la terza è
  // nuova: «l'hai rifiutata tu» non è «non ce l'hanno». Prima una riga rifiutata
  // finiva in `senzaMotivo` e la pagina scriveva di quel fornitore «non ce
  // l'hanno nel listino di adesso» — falso, e proprio sul prodotto in cui
  // l'utente aveva appena detto il contrario.
  // ⚠ E la quarta, che il 4 settembre 2026 si vedeva sul PC del negozio: la
  // riga proposta di quel fornitore è QUI SOPRA, con codice, EAN e prezzo — o
  // nel riquadro della risposta che gli hai già dato — e sotto la tabella c'era
  // scritto «non ce l'hanno nel listino di adesso». Due frasi opposte sullo
  // stesso fornitore a due centimetri di distanza. Chi ha un candidato in
  // pagina è già nominato là: qui non si conta.
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
              // Mezzo centesimo di tolleranza: sotto, la differenza si
              // scriverebbe «+0,00 €/pz», che è rumore.
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
      ${/* ⚠ La riga che spiega quanto dura un no si scriveva DENTRO ogni
           riquadro: su un prodotto rifiutato a due fornitori la stessa frase di
           una settimana usciva due volte di fila, con l'unica differenza del
           nome. La regola è una sola e vale per tutti: si dice una volta, sotto.
           Nel riquadro resta quello che cambia da fornitore a fornitore — la
           riga rifiutata, il giorno, e il modo di tornare indietro. */ ""}
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

// La via d'uscita quando l'abbinamento automatico non ce l'ha fatta, e non
// poteva farcela: il nome del gestionale non dice la variante, il fornitore usa
// un altro codice a barre, e nessun punteggio può indovinare. Si apre sul
// fornitore che ha già una riga per questo prodotto — così si controlla che sia
// davvero la stessa merce — oppure sul primo listino, per cercarla.
function renderApriIlListino(product) {
  if (mode === "demo") return "";
  // Prima il fornitore scelto, e solo se quello non ha una riga abbinata si
  // ripiega sul primo utile. `.find()` da solo prendeva sempre il primo
  // dell'elenco, che e' nell'ordine fisso di build_review_data.supplier_ids()
  // — betulla, larice, noce, cipresso — e coincideva con il fornitore scelto
  // solo per caso: il pulsante apriva il listino di un altro.
  const scelta = selectedOffer(product);
  const conRiga = (scelta && scelta.sourceRow != null ? scelta : null)
    || product.offers.find((offer) => offer.available && offer.sourceRow != null);
  // Quando nessuno ha una riga abbinata — il caso «cercala a mano» — vale la
  // stessa preferenza: si apre il listino del fornitore scelto, non il primo.
  const primo = conRiga || scelta || product.offers[0];
  // ⚠ Il pulsante si è chiamato «Sfoglia il listino LARICE» per un giorno, con
  // dentro il nome del fornitore che avrebbe aperto. Daniele l'ha guardato il
  // 22 agosto 2026 e l'ha tolto: il comando **non apre un listino solo**. La
  // finestra che si apre porta il nome per titolo e una tendina con tutti i
  // fornitori, e da lì si passa da uno all'altro senza chiuderla — quindi il
  // nome sul pulsante prometteva meno di quello che il comando fa, e lo
  // prometteva venti volte per pagina, una per scheda.
  //
  // Quale listino si apre PER PRIMO resta la scelta fatta qui sopra — il
  // fornitore scelto, non il primo dell'elenco — e quella non si tocca: è la
  // ragione per cui il pulsante non apre più la riga di qualcun altro.
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

// Il servizio locale somma due provenienze quando decide se serve una conferma:
// il prodotto (abbinamento incerto) e l'offerta scelta (match non esatto,
// espositore non ad alta confidenza). Qui va usata la stessa regola, altrimenti
// esiste uno stato senza uscita: il salvataggio viene rifiutato e nella pagina
// non c'è nessuna casella per dare la conferma che il servizio pretende.
function confirmationRequired(product) {
  // ⚠ Senza un fornitore scelto non c'è niente da confermare, ed è alla lettera
  // la riga di confine del servizio (`validate_snapshot`: «requires =
  // bool(supplier_id) and offer_needs_confirmation(...)»). Finché mancava,
  // `product.requiresConfirmation` da solo bastava a tenere in piedi il
  // bloccante «Conferma richiesta» su un prodotto che non ha più nessuna
  // offerta da confermare: la casella non c'era da nessuna parte e il blocco
  // restava.
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

// La conferma già data, quando c'è: chi l'ha ricevuta la conserva nel
// magazzino (`app/conferme.py`), e il servizio la rimette sul prodotto come
// `confirmation: {supplierId, since, article}`.
//
// ⚠ Senza questa riga la pagina non sapeva dire due cose che decidono se
// fidarsi della spunta: **quando** è stata data, e che vale **per l'articolo**
// — codice a barre più nome — e non per la riga del listino. È il motivo per
// cui sopravvive al listino della settimana dopo, quando quello stesso
// prodotto ha un'altra riga e un altro numero: senza dirlo, una spunta già
// messa somiglia a un residuo da controllare, e la si toglie per prudenza.
//
// E la revoca. Lato servizio funziona da sempre — togliere la spunta — ma era
// un gesto che nessuna scritta spiegava, ed è l'unica via d'uscita da una
// conferma sbagliata. Nessun pulsante nuovo: si nomina quello che c'è già.
function renderConfermaGiaData(product) {
  const conferma = product.confirmation;
  // ⚠ Anche il flag, non solo il ricordo: appena l'utente toglie la conferma
  // quella nel magazzino c'è ancora — sparisce al salvataggio — e scrivere
  // «già confermato» sopra una domanda riaperta direbbe il contrario di quello
  // che si vede. Tolta la conferma la domanda è di nuovo aperta, e quello che
  // serve leggere è il motivo per cui è stata fatta.
  if (!conferma || !product.confirmed) return "";
  const giorno = formatDayMonth(conferma.since);
  return `
    <p class="confirmation__gia">
      <strong>Già confermato da te${giorno ? ` il ${escapeHtml(giorno)}` : ""}.</strong>
      Vale per l’articolo — codice a barre e nome —, non per la riga del listino:
      resta valida anche con il listino della settimana prossima.
    </p>`;
}

// Che cosa succede DAVVERO a questo prodotto se la risposta è no.
//
// ⚠ Qui c'era un paragrafo di 293 caratteri che valeva per tutti i prodotti e
// per nessuno: «se non ce l'ha nessun altro, il prodotto finisce nell'elenco
// Prodotti da reperire» chiedeva a chi ordina di sciogliere un «se» che la
// pagina scioglie da sé — le offerte sono qui, e chi resta si conta. E finiva
// con «Si torna indietro dal riquadro di LARICE qui sopra», che nel momento in
// cui la si legge è falsa: quel riquadro nasce DOPO il no. Su SUPERMICIONE, il
// 22 agosto 2026, la frase nominava BETULLA mentre il riquadro sopra era di
// LARICE, e chi guardava in su non trovava niente.
//
// La regola di chi subentra è la stessa di `normalizeReview`: il più
// conveniente AL PEZZO fra i disponibili. Non se ne scrive una seconda: due
// autorità sullo stesso valore si contraddicono il giorno in cui una cambia.
function conseguenzaDelNo(product, offer) {
  const prossimo = asArray(product?.offers)
    .filter((candidato) => candidato.available && candidato.supplierId !== offer.supplierId)
    .sort((a, b) => a.pricePerPiece - b.pricePerPiece)[0];
  if (!prossimo) {
    // «L'unico che ce l'ha» sarebbe falso proprio nel caso in cui questa frase
    // si legge di più: gli altri la riga ce l'hanno, sei stato tu a dire che è
    // un altro articolo. Quello che conta è che è l'ultimo rimasto.
    return `${offer.supplierName} è l’ultimo fornitore rimasto su questo prodotto: senza di lui resta `
      + "senza fornitore, con la sua quantità, e alla compilazione finisce nell’elenco «Prodotti da reperire».";
  }
  return `${offer.supplierName} esce da questo prodotto e l’ordine passa a ${prossimo.supplierName}, `
    + `${formatEuro(prossimo.pricePerPiece)} al pezzo.`;
}

// La domanda dell'abbinamento, con le sue due risposte.
//
// ⚠ Fino al 22 agosto 2026 il sì era una **casella** e il no un **pulsante**:
// una domanda sola, due modi di rispondere. La casella metteva un valore nello
// stato e aspettava il salvataggio differito — nessuna attesa dichiarata,
// nessun riscontro — mentre il pulsante agiva, aspettava e lo diceva. La forma
// giusta ce l'aveva già il gemello di questa scheda
// (`renderCandidateDecisions`, «È lo stesso prodotto? Sì / No»), e adesso è la
// stessa: due pulsanti, la stessa attesa, le stesse parole.
//
// A risposta data la domanda non si ripropone: si dice che cosa è stato
// risposto e si lascia una via per cambiare idea, che è esattamente come si
// comporta il no nella griglia delle offerte.
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
        ${/* Il motivo è la cosa che serve per decidere: nascosto dietro «Perché
             serve?» chiedeva di rispondere prima di sapere a che cosa. A
             risposta già data non serve più: la domanda non è più aperta. */ ""}
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
          ${/* ⚠ Il sì per primo, e in evidenza: è la risposta che chiude la
               domanda senza togliere niente a nessuno. */ ""}
          <button type="button" class="button button--primary button--piccolo" data-action="conferma-abbinamento"
            data-focus-key="conferma-${escapeHtml(product.id)}"
            data-product-id="${escapeHtml(product.id)}" data-confermato="true"
            ${occupato ? "disabled" : ""}>${attendeIlSi ? "Attendere…" : "Sì, è lo stesso"}</button>
          ${/* La seconda risposta possibile. Senza di lei la domanda ne ammetteva
               una sola: chi non poteva confermare restava con la compilazione
               ferma su «Conferma richiesta · bloccante», e le due uscite
               apparenti — confermare lo stesso, o «Escludi dall'ordine» — sono un
               ordine sbagliato e un prodotto che sparisce anche dall'elenco di
               quelli da reperire, perché quello salta chi ha quantità zero.
               In modalità dimostrativa i comandi non ci sono: il servizio non
               risponde e un pulsante che dà errore ogni volta che lo si preme non
               è una domanda. */ ""}
          ${!offer ? "" : `
          <button type="button" class="button button--secondary button--piccolo" data-action="rifiuta-abbinamento"
            data-focus-key="rifiuto-${escapeHtml(product.id)}-${escapeHtml(offer.supplierId)}"
            data-product-id="${escapeHtml(product.id)}" data-supplier-id="${escapeHtml(offer.supplierId)}"
            aria-describedby="rifiuto-nota-${escapeHtml(product.id)}"
            data-rifiutata="true" ${occupato ? "disabled" : ""}>${
              /* ⚠ Il pulsante si spegneva e basta: fra la pressione e la
                 pagina rifatta — un salvataggio, la risposta e il confronto
                 riletto — restava grigio senza dire che stava lavorando. Si
                 aspetta con le stesse parole in tutti e due i versi, e si
                 guarda QUESTA risposta: mentre un'altra scheda risponde, questo
                 pulsante è spento ma non sta attendendo niente. */ ""
            }${attendeIlNo ? "Attendere…" : "No, non è lo stesso"}</button>
          <span id="rifiuto-nota-${escapeHtml(product.id)}">${escapeHtml(conseguenzaDelNo(product, offer))}</span>`}
        </p>`}
      </div>
    </div>`;
}

// Lo sconto di testata del fornitore, in percentuale. Il campo sta dove stanno
// i suoi soldi — nella fascia dei totali — perché è lì che si vede l'effetto:
// si scrive 6 e i prezzi di quel fornitore scendono ovunque, comprese le
// assegnazioni, che il servizio rifà da sé sul più conveniente.
function renderStickySupplierTotals() {
  const sconti = state.review?.supplierDiscounts || {};
  return `
    <aside class="supplier-totals-strip" aria-label="Totali ordine per fornitore">
      <span class="supplier-totals-strip__label">Totali ordine</span>
      ${supplierTotals().map((entry) => {
        // Gia' un numero: lo riduce a percentuale `normalizeSupplierDiscounts`,
        // che e' il posto dove i dati del servizio diventano dati della pagina.
        // Rifare il conto qui vorrebbe dire due autorita' sullo stesso valore,
        // e la seconda coprirebbe i difetti della prima senza dirlo.
        const sconto = sconti[entry.supplier.id] || 0;
        return `<span class="supplier-totals-strip__item${entry.total > 0 ? " is-active" : ""}${sconto ? " has-discount" : ""}">
          <span>${escapeHtml(entry.supplier.name)}</span>
          <strong>${formatEuro(entry.total)}</strong>
          ${/* ⚠ L'etichetta e' visibile e non piu' solo in un `title`: il
               suggerimento compare dopo un secondo di sosta del MOUSE, quindi
               chi lavora con la tastiera o su tocco non lo vedeva mai. Restava
               un `aria-label` per il lettore di schermo, e per l'occhio un
               riquadrino con «−» e «%» attorno da cui dedurre. La dimensione
               resta quella che e': e' una decisione scritta nel foglio di
               stile, non una svista. */ ""}
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
      ${/* Il totale stava in un badge dentro l'intestazione dell'elenco, che
           scorre via alla prima rotellina. Qui resta sotto gli occhi mentre si
           cambiano le quantità, che è quando lo si guarda. È la somma delle
           voci qui accanto: non un numero in più, lo stesso numero in un posto
           solo. */ ""}
      <span class="supplier-totals-strip__item supplier-totals-strip__item--totale">
        <span>Totale</span>
        <strong>${formatEuro(allOrderTotal())}</strong>
      </span>
    </aside>`;
}

// ⚠ Qui c'era la riga «Totale 3.450,88 € — le righe mostrate sommano
// 3.450,87 €, 0,01 € in meno per gli arrotondamenti al centesimo», sotto il
// totale di ogni fornitore e sotto quello generale. Daniele l'ha letta usando
// il programma il 20 agosto 2026: «ti sembra utile in qualche modo, visto che
// il totale è scritto di fianco al fornitore? INUTILE». Ha ragione — un
// centesimo di scarto da arrotondamento non cambia nessuna decisione di chi
// ordina, e occupava una riga proprio accanto al numero che invece conta.
//
// Il servizio locale continua a mandare `roundingDifference` in `orderSummary`
// (server.py) e continua a essere provato lì: il dato resta per chi controlla
// i conti a mano, non si stampa più sulla pagina.

// ⚠ L'elenco dei prodotti si richiude cliccando sul nome del fornitore, e il
// suo subtotale resta in vista. Con cinquecento righe, per arrivare al
// fornitore dopo si scorreva tutto quello che c'era in mezzo (Daniele, 22
// agosto 2026, usando il programma).
//
// Nasce APERTO — è come si legge oggi, e chiudere di suo un riepilogo
// nasconderebbe la cosa che si è venuti a guardare — quindi la memoria deve
// tenere il «chiuso», non l'«aperto»: la chiave sta in `state.aperti` come
// tutte le altre, con `apribile(chiave, true)`. Senza, il riepilogo si
// riaprirebbe da solo al primo cambio di quantità, che lo ridisegna tutto.
//
// La chiave porta dentro l'identificativo del fornitore: due fornitori non si
// aprono e chiudono insieme.
function renderSupplierSummary(entry) {
  if (!entry.products.length) return "";
  const reached = entry.minimumOrder <= 0 || entry.total >= entry.minimumOrder;
  const products = entry.products.slice().sort((a, b) => {
    if (state.summary.sort === "total") return b.subtotal - a.subtotal;
    return a.product.name.localeCompare(b.product.name, "it", { sensitivity: "base" });
  });
  return `
    <details class="supplier-summary" ${apribile(`riepilogo-fornitore:${entry.supplier.id}`, true)}>
      ${/* Il conto delle righe sta accanto al nome, ed è quello che dice che
           cosa si sta nascondendo quando il riquadro è chiuso: chiuso si legge
           «BETULLA · 12 righe · 340 unità d'ordine» e il suo totale. */ ""}
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

// --- Sposta tutto su un altro fornitore (pagina 3) ------------------------
// Il caso vero: "da Larice avrei fatto 200 euro, troppo poco, sposto i suoi
// prodotti sul primo miglior altro venditore". La differenza di costo si vede
// PRIMA di decidere, ma non la calcola il browser: ogni cifra mostrata qui
// arriva da POST /api/suppliers/move-preview. Il numero di colli non cambia,
// cambia solo il fornitore, quindi il prezzo e i pezzi effettivi.

// I motivi arrivano dal servizio locale come codici: qui si traducono in
// italiano corrente. Un codice sconosciuto non deve mai far sparire il nome del
// prodotto, perciò c'è sempre una frase di ripiego.
// Questi sono gli unici due motivi che il servizio locale emette davvero: se
// qui compaiono chiavi che il programma non manda mai, il prodotto finisce
// nella frase di riserva e l'utente non sa perché è rimasto dov'era.
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
    // Gli omaggi delle soglie: quanti se ne perdono e quanti se ne guadagnano.
    // Il NUMERO lo conta il servizio locale con le regole delle promozioni; il
    // valore dell'omaggio non si calcola, per decisione commerciale.
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
    // Letto ma non mostrato: il contratto non dice se è il totale dell'ordine
    // intero o del solo fornitore di partenza, e una cifra con l'etichetta
    // sbagliata sarebbe peggio di una cifra in meno. Il totale del fornitore che
    // si sta svuotando è già nel titolo, preso dalla scheda che l'utente vede.
    currentNetTotal: finiteNumber(payload?.currentNetTotal, 0),
    // Una destinazione che non sposta niente non è una scelta: non si propone.
    options: asArray(payload?.options)
      .map(normalizeMoveOption)
      .filter((option) => option.movedCount > 0 && option.assignments.length > 0),
  };
}

// La differenza va mostrata anche quando è a favore. Il colore da solo non
// basta: accanto alla cifra c'è sempre la frase che dice cosa significa.
// La differenza di spesa DA SOLA inganna, ed è la stessa trappola del confronto
// fra offerte: i colli di fornitori diversi non contengono lo stesso numero di
// pezzi, quindi un'opzione che fa spendere meno può consegnare metà merce. Si
// dicono sempre tutte e due le cose, e la parola "si risparmia" compare solo
// quando la merce non cala.
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

// Il numero onesto per confrontare due opzioni è quanto costa ogni singolo
// pezzo: è l'unico che non dipende da quanta merce contiene un collo.
function movePieceTexts(option) {
  const pieces = Math.trunc(finiteNumber(option?.deltaPieces, 0));
  const goods = pieces === 0
    ? `stessa merce: ${formatInteger(option.deliveredPiecesAfter)} pezzi`
    : `merce: da ${formatInteger(option.deliveredPiecesBefore)} a ${formatInteger(option.deliveredPiecesAfter)} pezzi (${pieces > 0 ? "+" : "−"}${formatInteger(Math.abs(pieces))})`;
  const unit = `al pezzo: da ${formatEuro(option.costPerPieceBefore)} a ${formatEuro(option.costPerPieceAfter)}`;
  return { goods, unit };
}

// Le soglie sono il motivo per cui si sposta: si dice sempre, con il totale
// ricevuto dal servizio locale, chi la raggiunge e chi no. Attenzione a
// hadOrderBefore: il servizio dichiara "soglia raggiunta" anche per un
// fornitore che non ordina niente, perché chi non ordina non è sotto soglia.
// Senza quel controllo si finisce a scrivere che un fornitore partito da zero
// "è sceso e non raggiunge più" una soglia che non aveva mai avuto.
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

// Gli omaggi che si perdono spostando la merce. Le soglie con omaggio si
// raggiungono con l'ordine di UN fornitore: portando i prodotti altrove la
// soglia salta, e finora il preventivo non lo diceva. Si dice il NUMERO degli
// omaggi e mai il loro valore: quanto vale un omaggio non lo decide questo
// programma (decisione commerciale del 12 agosto 2026).
function moveGiftNotes(option) {
  const notes = [];
  const persi = Math.max(0, Math.trunc(finiteNumber(option?.giftsLost, 0)));
  const guadagnati = Math.max(0, Math.trunc(finiteNumber(option?.giftsGained, 0)));
  // Il dettaglio per fornitore: «2 prima, 2 dopo» senza dire CHI li perde e
  // chi li guadagna nascondeva uno scambio fra fornitori — sono merci
  // diverse (revisione avversariale R4). Il servizio conta persi e
  // guadagnati per fornitore, la pagina li mostra per fornitore.
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
  // I prodotti che restano indietro si dicono per nome: un numero da solo non
  // permette di capire che cosa si sta lasciando dov'era.
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

// Il comando sta su ogni scheda fornitore. ⚠ Sotto soglia diventava blu, cioè
// il primario della pagina 3, e con quattro fornitori sotto soglia sarebbero
// stati quattro pulsanti primari contro il vero passo successivo, che è
// «Compila i listini». Adesso resta secondario e a segnalare la soglia è il
// colore del riquadro (`is-below`).
//
// La frase è una sola e dice che cosa fa il comando. Quella di prima —
// «Con BETULLA il minimo d'ordine di 1000,00 € non è raggiunto» — ripeteva
// l'intestazione della scheda, due righe più su, che scrive già «mancano
// 25,65 € al minimo d'ordine».
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
  // Le note sulle soglie si mostrano TUTTE, comprese quelle che avvisano che la
  // soglia resta non raggiunta: nasconderle lasciava in vista solo le buone
  // notizie, e la soglia è il motivo per cui si sta spostando. Con loro vanno
  // gli omaggi persi, che della soglia sono la conseguenza.
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
          // Un espositore si ordina a espositori: scrivere "colli" qui sopra un
          // espositore è la stessa confusione che il servizio ha appena smesso
          // di fare sui numeri.
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
    // Nell'esempio non c'è un servizio locale a cui chiedere il preventivo, e le
    // differenze non le inventa il browser: si dice com'è invece di mostrare
    // numeri finti.
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

// Dopo lo spostamento: si dice che cosa è successo, si dice se una destinazione
// ha raggiunto la soglia e si lascia il comando per tornare indietro.
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

// L'annullo del «×» del riepilogo. Sta accanto a quello dello spostamento fra
// fornitori, in cima alla pagina 3 e non dentro l'elenco: la riga da cui il
// prodotto è appena sparito non esiste più, e una barra infilata nell'elenco
// farebbe scorrere quello che si sta guardando. In cima resta ferma e non
// spinge giù il pulsante «Compila», che sta in fondo.
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

// writerIssues e historyIssues arrivano dal servizio locale da un pezzo e la
// pagina non li leggeva: l'unico segnale era quello che il backend infilava
// dentro `message`, stampato accanto al pulsante verde «Scarica i listini
// pronti per l'invio». Una compilazione che ha scartato una copia, o che non è
// entrata fra gli ordini da controllare la settimana prossima, non è una
// compilazione pienamente riuscita e non va presentata come tale.
function renderCompileIssues(titolo, voci) {
  const elenco = asArray(voci).map((voce) => userFacingText(voce)).filter(Boolean);
  if (!elenco.length) return "";
  return `
    <div class="results__issues-block">
      <strong>${escapeHtml(titolo)}</strong>
      <ul class="results__issues">${elenco.map((voce) => `<li>${escapeHtml(voce)}</li>`).join("")}</ul>
    </div>`;
}

// La compilazione andata male. Le scelte sono salvate e il pulsante qui sopra
// riprova davvero: la frase può dirlo senza mentire.
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

// «Premo Compila i listini e non succede niente»: il pulsante si spegne e per
// alcuni secondi la pagina non dice altro, quindi non sembra un programma che
// lavora — sembra un programma rotto, e la reazione naturale è premere di
// nuovo (Daniele, 22 agosto 2026).
//
// ⚠ La barra è INDETERMINATA di proposito, e non è un ripiego: la scrittura
// vera la fa un processo Node (`scripts/write_supplier_orders.mjs`) che non
// riporta avanzamenti. Una percentuale qui sarebbe inventata. Quello che
// serve dire è «sto lavorando», e una barra che si muove lo dice; per quanto
// manchi non c'è nessuno da cui prendere la risposta, e infatti la frase non
// lo promette. ⚠ E non lo spiega nemmeno: il perché non manca la stima è una
// cosa che serve a chi legge questo codice — sta scritta qui — non a chi sta
// in negozio davanti alla schermata. A lui servono tre cose: sto lavorando,
// non chiudere, non premere di nuovo. La frase dice quelle, e in più dice
// dove guardare fra un attimo, che è dove comparirà l'esito.
//
// La catena del ricalcolo ha invece una percentuale vera (`avanzamento.percento`)
// perché conta le fasi: se un giorno il writer dichiarasse le sue, questa barra
// diventerebbe quella. Fino ad allora, no.
function renderCompileProgress(quante) {
  if (!state.compiling) return "";
  return `
    <div class="compile-progress">
      <div class="compile-progress__bar" role="progressbar" aria-label="Compilazione dei listini in corso"></div>
      ${/* La frase è quello che resta a chi ha chiesto meno movimento, dove la
           barra non c'è: deve reggere da sola. */ ""}
      <p class="compile-progress__nota" role="status">Sto scrivendo ${escapeHtml(contati(quante, "copia", "copie"))} del listino. Non chiudere il programma: quando ho finito te lo dico qui sotto.</p>
    </div>`;
}

function renderCompileResult() {
  if (!state.compileResult) return renderCompileFailure();
  const outputs = asArray(state.compileResult.outputs);
  // ⚠ Qui c'era anche un elenco di errori **per fornitore**, tradotto da un
  // codice (`FORNITORE_SENZA_COPIA`) che il servizio locale non ha mai mandato:
  // una compilazione riuscita non ha errori per fornitore, perché o si compila
  // tutto o `run_writer` si ferma e la risposta è un guasto (vedi
  // `renderCompileFailure`). Tolto il 17 agosto 2026: era codice vivo solo nel
  // suo test, e quello che davvero arriva sta in `writerIssues`.
  const writerIssues = asArray(state.compileResult.writerIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  // Gli avvisi della consegna: il documento c'è ed è giusto, ma qualcosa attorno
  // non è andato — di solito il nome leggibile che non si è potuto dare perché
  // il file era aperto. Prima stavano solo dentro `message` e il riquadro
  // restava verde, come se fosse andato tutto liscio.
  const deliveryIssues = asArray(state.compileResult.deliveryIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  const historyIssues = asArray(state.compileResult.historyIssues).map((voce) => userFacingText(voce)).filter(Boolean);
  const parziale = Boolean(writerIssues.length || deliveryIssues.length || historyIssues.length);
  // Il pulsante della consegna compare soltanto quando il servizio locale manda
  // uno zip: senza listini non c'è niente da consegnare, e un pulsante che
  // scarica un 404 è peggio di nessun pulsante. È un <a download> e non un
  // <button>: funziona senza altro JavaScript e l'indirizzo si legge nella barra
  // di stato prima di premerlo.
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

// Gli stati che l'audit registra. Un valore che non è in tabella si mostra
// com'è arrivato: inventare una traduzione nasconderebbe un cambio del
// contratto invece di farlo vedere.
const STATI_COMPILAZIONE = {
  FILES_READY: "listini pronti",
  PLAN_READY: "solo il piano, nessun listino",
  SCONOSCIUTO: "stato non registrato",
};

const TIPI_FILE_COMPILAZIONE = {
  listino: "listino pronto",
  // Senza questa voce il file comparirebbe nell'elenco senza dire che cos'è, e
  // un foglio chiamato «Prodotti da reperire» in mezzo ai listini si scambia
  // per un listino — cioè per qualcosa da mandare a un fornitore.
  da_reperire: "prodotti da cercare altrove",
  piano: "piano dell’ordine",
  altro: "altro documento",
};

// La riga di dettaglio sotto la data. I numeri arrivano dal servizio locale e si
// stampano come sono: il browser non ricalcola né totali né righe.
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

// Una compilazione dell'elenco. La voce con `completa: false` — l'audit non si
// legge — si mostra lo stesso e lo dichiara: la cartella esiste, i suoi file si
// scaricano, e nasconderla farebbe sparire dei listini che invece ci sono.
function renderCompilazione(voce) {
  const cartella = String(voce?.cartella || "");
  const etichetta = String(voce?.etichetta || cartella || "Compilazione senza data");
  const completa = voce?.completa !== false;
  const totale = voce?.totaleNetto;
  const fornitori = asArray(voce?.fornitori);
  const documenti = asArray(voce?.file);
  const zipNome = String(voce?.zipNome || "");
  const zipUrl = safeDownloadUrl(voce?.zipUrl);
  // I documenti che la compilazione aveva prodotto e che nella cartella non ci
  // sono più: quasi sempre è l'utente che li ha spostati per allegarli a una
  // mail. Va detto lo stesso, perché altrimenti chi cerca un listino di due
  // settimane fa non capisce se l'ha spostato lui o se il programma l'ha perso.
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

// Passo 3: le compilazioni già fatte, dalla più recente. L'ordine è quello che
// manda il servizio locale, che legge le date sul disco.
function renderCompilazioniPrecedenti() {
  if (mode === "demo") return "";
  const storico = state.compilazioni;
  const voci = asArray(storico.elenco);
  // Finché la prima risposta non è arrivata l'elenco è vuoto perché non l'ha
  // ancora letto nessuno, non perché non ci siano compilazioni: dire «nessuna
  // compilazione registrata» a chi ne ha appena fatte dieci sarebbe una bugia
  // lunga il tempo di una richiesta.
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
        ${/* ⚠ Tre numeri e nient'altro erano tre motivi per agire senza un modo
             di agire: «Ci manca poco: 3» non dice di quali fornitori parla
             ne' porta da nessuna parte. La versione utile della stessa
             informazione esiste gia', ed e' in pagina 2 — «Offerte a portata di
             mano», con il comando «Mostra i prodotti». La riga qui sotto dice
             dove andare a farci qualcosa. */ ""}
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
  // ⚠ Qui si leggevano 81 avvisi. Ottanta erano di prodotto — «Possibile
  // prodotto CIPRESSO» trentanove volte, e trentadue «Nessuna offerta
  // utilizzabile» (il titolo di allora: oggi è «Nessun fornitore ce l'ha») —
  // e sono gli stessi che stanno sulla scheda di ogni prodotto in pagina 2,
  // dove c'è il pulsante per rispondere. Due righe di riepilogo li contano già
  // — «Prodotti che nessun fornitore ha» e «Proposte da controllare» — e
  // dicono con quale filtro si trovano, anzi dal 20 agosto 2026 ci portano con
  // un comando: ripeterli uno per uno qui non aggiungeva un'azione,
  // aggiungeva ottanta scatole da scorrere per arrivare ai numeri dell'ordine.
  // Restano gli avvisi che riguardano l'ordine intero — i riepiloghi, i minimi
  // d'ordine, le righe di listino lette con riserva — e i bloccanti, che
  // restano nominati prodotto per prodotto nel riquadro sopra.
  const notices = issues.filter((issue) => !issue.blocking && !issue.productId);
  const hasOrders = orderedProducts().length > 0;
  // ⚠ Non si compila mentre la catena sta ricalcolando. Il servizio adesso lo
  // rifiuta, e il pulsante deve dirlo PRIMA: finche' la fase 9 non sostituisce
  // il confronto, quello che si compilerebbe sono i listini di quello di
  // prima — prezzi della settimana scorsa — mentre in pagina 1 una barra dice
  // che si sta aggiornando. Basta far partire il ricalcolo e venire qui.
  const ricalcoloInCorso = pipelineInCorso();
  const canCompile = hasOrders
    && blockers.length === 0
    && (belowThreshold.length === 0 || state.acceptBelowThreshold)
    && !state.compiling
    && !ricalcoloInCorso;

  return `
    ${pageHeading("3. Riepilogo e compilazione", "Controlla prodotti, quantità, fornitori, subtotali e minimi d’ordine prima di creare i listini.")}
    ${/* La stessa fascia della pagina 2, nello stesso punto: subito sotto il
         titolo, agganciata in alto. Qui serve di più che di là — è la pagina in
         cui si decide se l'ordine si manda, e i subtotali sparivano alla prima
         rotellina, dentro schede di fornitore alte quanto i loro prodotti.
         È il componente, non una copia: un totale scritto due volte in due
         posti è un totale che prima o poi dice due numeri.
         ⚠ Si aggancia a `top: calc(3rem + 4px)`, sotto la barra dei passi.
         In questa pagina non c'è nient'altro di agganciato in alto — le sole
         due quote del foglio sono quella barra e questa fascia — quindi non si
         sovrappone a niente; quello che le scorre sotto passa e basta. */ ""}
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
        ${/* «Pronto per creare i listini?» era una domanda a cui rispondeva il
             pulsante due righe sotto, e «I documenti originali restano
             invariati» è la stessa promessa scritta in testa alla pagina 1.
             Resta la frase che dice la cosa che nessun altro dice. */ ""}
        <h3>Compilazione dei listini</h3>
        <p>Creo una copia del listino di ogni fornitore, con le quantità che hai scelto. <strong class="no-send-message">Nessun ordine viene inviato.</strong></p>
      </div>
      <div class="compile-card__total">
        <span>Totale dell’ordine</span>
        <strong>${formatEuro(allOrderTotal())}</strong>
      </div>
      ${/* Quante copie, e per chi. E' l'unica azione della pagina che lascia un
           segno fuori dal programma, e prima di premere non si sapeva quanti
           documenti tra un attimo sarebbero esistiti. Solo conteggio e nomi:
           i subtotali per fornitore stanno gia' nel riquadro qui sopra, e da
           li' sono stati tolti ottanta avvisi duplicati proprio per non far
           leggere due volte la stessa cosa. */ ""}
      ${activeTotals.length ? `<p class="compile-card__copie">${escapeHtml(`${activeTotals.length === 1 ? "Sarà creata" : "Saranno create"} ${contati(activeTotals.length, "copia", "copie")}: ${elencoNomi(activeTotals.map((entry) => entry.supplier.name))}.`)}</p>` : ""}
      ${belowThreshold.length ? `
        <label class="confirmation">
          <input type="checkbox" data-below-threshold-confirm data-focus-key="sotto-minimo" ${state.acceptBelowThreshold ? "checked" : ""}>
          <span><strong>Confermo gli ordini sotto il minimo.</strong> ${escapeHtml(belowThreshold.map((entry) => entry.supplier.name).join(", "))}: i listini saranno preparati anche se il minimo d’ordine non è raggiunto.</span>
        </label>` : ""}
      ${/* Era verde e in maiuscolo, e sopra al riepilogo c'era un altro
           pulsante blu («Sposta tutto su un altro fornitore», in evidenza sotto
           il minimo d'ordine): due comandi che si contendevano l'occhio, e
           quello che grida non era il passo successivo — questo lo è. Il blu è
           lo stesso delle pagine 1 e 2, così «il comando principale» si legge
           allo stesso modo in tutte e tre. */ ""}
      ${/* ⚠ A compilazione riuscita questo pulsante restava blu, largo e acceso
           SOPRA il riquadro dell'esito, mentre il passo successivo — scaricare
           lo zip da portare al fornitore — era il pulsante piu' piccolo e piu'
           in basso. Il piu' grande, in alto, ricompila e crea un'altra cartella
           datata. Due comandi primari nella stessa schermata, nel punto in cui
           il lavoro finisce. Fatta la compilazione, questo diventa un comando di
           contorno e dice la conseguenza invece del passo. */ ""}
      <button class="button button--${state.compileResult ? "secondary" : "primary"} button--wide" type="button" data-action="compile" ${canCompile ? "" : "disabled"}>
        ${state.compiling ? "Compilazione in corso…" : ricalcoloInCorso ? "Il confronto si sta aggiornando…" : state.compileResult ? "Rifai i listini" : "Compila i listini"}
      </button>
      ${ricalcoloInCorso ? `<p class="compile-card__copie">Quando il confronto ha finito di aggiornarsi, il comando torna: adesso i listini nascerebbero con i prezzi di prima.</p>` : ""}
      ${/* Sotto il pulsante, dove l'occhio è appena stato: sopra sarebbe
           comparsa mezza schermata più in su di dove si è premuto. */ ""}
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

// Le voci che si cambiano da qui, con il nome che legge una persona e la
// spiegazione di che cosa succede se le si sposta. L'elenco è lo stesso di
// VOCI_IMPOSTAZIONI nel servizio locale: quello che non è scritto qui non è
// una preferenza ma un componente del programma — la versione del prompt, per
// esempio, è stata scelta misurando quanti ALTA sbagliati produce.
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

// I prezzi di OpenRouter sono per singolo token: al milione diventano numeri
// che una persona può confrontare a occhio.
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
      ${/* Secondario, e non primario: in questa pagina i pulsanti blu erano due —
           questo a meta' e «Salva modello e limiti» in fondo — e nessuno dei
           due salva quello che salva l'altro. Il primario di una pagina e' uno,
           ed e' quello in fondo, dove sta in tutte e tre le pagine del lavoro.
           L'etichetta dice gia' che cosa salva, quindi non cambia. */ ""}
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

// Le due fasce di annullo che riguardano le quantita' — il «×» del riepilogo e
// l'azzeramento in blocco — non possono restare disponibili dopo che l'utente ha
// rimesso mano ai numeri: premerle riscriverebbe quello che ha appena scritto.
// È la stessa ragione per cui `goToStep()` invalida l'annullo dello spostamento
// fra fornitori, con il commento che lo dice.
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

// Il numero di colli resta invariato quando si cambia fornitore, ma se il nuovo
// fornitore ha pezzi per collo diversi la merce effettiva cambia a parità di colli
// digitati: evidenzia la scheda per qualche secondo e lo dichiara esplicitamente.
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
  // Chi tocca un passo del flusso sta uscendo dalle impostazioni, anche se non
  // ha premuto "Torna al lavoro": la chiave incollata e non salvata se ne va con
  // la pagina.
  state.impostazioni.aperta = false;
  state.impostazioni.nuovaChiave = "";
  if (nextStep !== state.currentStep) {
    // Fuori dal riepilogo il preventivo di spostamento non ha più senso, e
    // l'annulla non può restare disponibile dopo modifiche fatte a mano: si
    // ripristinerebbero assegnazioni che nel frattempo l'utente ha cambiato.
    state.supplierMove = emptySupplierMove();
    state.supplierMoveUndo = null;
    scadonoGliAnnulliDiQuantita();
  }
  const cambiaPasso = nextStep !== state.currentStep;
  state.currentStep = nextStep;
  state.runtimeError = "";
  // ⚠ Solo se il passo cambia davvero.  Premere «Riepilogo e compilazione»
  // mentre ci si e' gia' sopra — cioe' premere per sbaglio la voce accesa della
  // barra — buttava via `compileResult`, e con lui spariva «Scarica i listini»:
  // il collegamento ai documenti appena creati, nel momento in cui il lavoro
  // era finito. Restava cercarli fra le compilazioni precedenti.
  if (cambiaPasso) state.compileResult = null;
  scheduleSave();
  render();
  // All'ingresso nella pagina 2 si chiedono gli ordini ancora da ricevere: la
  // risposta arriva dopo e ridisegna solo se serve.
  if (nextStep === 2) loadPendingOrders();
  // All'ingresso nel riepilogo si chiede l'elenco delle compilazioni già fatte:
  // è lì che si ritrovano i listini di una settimana fa.
  if (nextStep === 3) loadCompilazioni();
  document.querySelector("#workspace")?.focus({ preventScroll: true });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function acceptedFile(file) {
  return /\.(xlsx|xls|csv)$/i.test(file.name);
}

// Perché quel documento è rimasto fuori, detto a chi lo ha trascinato. Il caso
// più frequente è il formato, ma un file senza estensione non ne ha uno da
// nominare e «non è un .xlsx» sarebbe una risposta a una domanda diversa.
function motivoDelloScarto(nome) {
  const punto = String(nome || "").lastIndexOf(".");
  const estensione = punto > 0 ? String(nome).slice(punto).toLowerCase() : "";
  return estensione
    ? `${estensione} non è un formato che so leggere: servono .xlsx, .xls o .csv`
    : "senza estensione: non so di che formato è. Servono .xlsx, .xls o .csv";
}

function addPendingFiles(fileList, role = "suppliers") {
  // Durante il ricalcolo aggiungere documenti non è un'attesa, è un risultato
  // diverso: il trascinamento va rifiutato come i pulsanti, e con la stessa frase.
  if (pipelineInCorso()) {
    showToast("Il confronto è in corso: i documenti si caricano appena ha finito.", "error");
    return;
  }
  const incoming = [...fileList];
  const rejected = incoming.filter((file) => !acceptedFile(file));
  if (rejected.length) {
    showToast(`${rejected.length === 1 ? "Ignorato" : "Ignorati"} ${contati(rejected.length, "documento", "documenti")}: il formato non è riconosciuto.`, "error");
    // ⚠ Il messaggio a comparsa resta, ma smette di essere l'unico posto in cui
    // l'informazione esiste: viveva 3,6 secondi, non nominava i file, e chi non
    // lo leggeva in tempo faceva il confronto senza un fornitore — cioè mandava
    // l'ordine a chi costa di più. Uno scarto che non si conta non è una
    // scelta: è una perdita di dati.
    const gia = new Set(state.fileScartati.map((voce) => voce.chiave));
    for (const file of rejected) {
      // ⚠ I file nascosti non si elencano. Trascinando una cartella dal Finder
      // arriva anche il `.DS_Store` che macOS ci semina dentro: non e' un
      // listino che l'utente voleva caricare, e comparire fra i «documenti
      // rimasti fuori» con una spiegazione sbagliata («senza estensione»)
      // sarebbe rumore su una riga che deve dire solo cose che contano.
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
  // Il pulsante è già spento durante il ricalcolo: questa è la stessa regola
  // detta dove conta, cioè prima di mandare i documenti al servizio locale.
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
      // ⚠ Il salvataggio PRIMA della richiesta, per la ragione scritta per
      // esteso in `abbinaLaRiga`: finito il caricamento la pagina rilegge il
      // confronto e SOSTITUISCE quello in pagina, e una quantità scritta meno di
      // 450 ms fa — o rimasta indietro dopo un salvataggio fallito — se ne
      // andrebbe con lui, cioè un ordine sbagliato senza che niente lo dica (6
      // settembre 2026). Con lo stato pulito `saveState()` torna vero subito.
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
// Il ricalcolo del confronto (pagina 1)
// --------------------------------------------------------------------------

// Un secondo: la catena deterministica dura venticinque secondi in tutto e la
// fase AI un paio di minuti, quindi è il ritmo con cui la barra si muove senza
// che le richieste diventino un carico.
const RITMO_PIPELINE_MS = 1000;
// Quante richieste di stato di fila possono andare a vuoto prima di dirlo. Una
// sola non è una notizia — il servizio locale può essere occupato un istante —
// cinque di fila, al ritmo qui sopra, sono cinque secondi di barra ferma senza
// una spiegazione.
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
      // ⚠ Niente ripiego su «__new__»: quando il servizio non propone nessun
      // fornitore la tendina resta su «Scegli il fornitore…». Preselezionare
      // qualcosa — un fornitore vero o «Nuovo fornitore…» — vuol dire che
      // premere Conferma senza guardare fa comunque qualcosa, e quel qualcosa
      // è stato sostituire il listino BETULLA con un foglio di offerte.
      supplierChoice: String(proposta.supplierId || ""),
      supplierName: "",
      sheet: proposta.sheet || documento.sheets?.[0]?.name || "",
      headerRow: Number(proposta.headerRow || 1),
      dataStartRow: Number(proposta.dataStartRow || Number(proposta.headerRow || 1) + 1),
      // Nessun separatore e' scelto in partenza: la regola la conferma
      // l'utente, guardando la riga nell'anteprima. Una regola indovinata dal
      // programma sarebbe peggio del numero che sostituisce.
      dataStartBreak: "",
      markerText: "",
      columns: { ...(proposta.columns || {}) },
      orderColumn: proposta.orderColumn || "",
      factorMode: haFattore ? "column" : "fixed",
      piecesPerCartonDefault: 1,
      // Vuoto vuol dire «ogni riga porta la sua offerta», che è come scrivono
      // quasi tutti: non è un valore mancante, è il predefinito, e il servizio
      // lo intende così. Indovinare la forma guardando il file sarebbe peggio
      // del numero che sostituisce.
      commercialLayout: "",
      // I valori della colonna «Disponibilità» li scrive l'utente guardando il
      // suo listino: qui non c'è niente da indovinare, e la voce c'è fin
      // dall'inizio perché «hai del lavoro da perdere» confronta questi stessi
      // valori con quelli di adesso.
      availableValues: "",
    };
  }
  state.schemaMapping.data = payload;
  state.schemaMapping.loadedRunId = String(payload.runId || "");
  state.schemaMapping.values = valori;
  // Com'erano appena caricati: serve a sapere se chi esce sta buttando via
  // qualcosa oppure sta solo tornando indietro.
  state.schemaMapping.valoriIniziali = JSON.stringify(valori);
  state.schemaMapping.chiedendoUscita = false;
  state.schemaMapping.results = {};
}

// Chi ha toccato qualcosa dentro «Rivedi le colonne» ha del lavoro da perdere.
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

// --- Rivedere le colonne di un listino già caricato -----------------------
// Il selettore è lo stesso della mappatura guidata; cambia da dove arriva
// l'anteprima e che cosa succede alla conferma. Qui non riparte niente: la
// mappatura diventa una decisione scritta, e la usa il prossimo confronto —
// che lo lancia l'utente.

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
    // ⚠ Dopo `initializeSchemaMapping`, che azzera lo stato: senza questa riga
    // la schermata si chiuderebbe da sola appena finito di aprirsi.
    state.schemaMapping.documento = nome;
  } catch (error) {
    state.schemaMapping.error = error.message || "Non sono riuscito ad aprire l’anteprima.";
  } finally {
    state.schemaMapping.loading = false;
    render();
  }
}

function chiudiColonneDocumento({ confermato = false } = {}) {
  // ⚠ «Torna ai documenti» buttava via tutto quello che si era impostato, in
  // silenzio e senza dirlo nell'etichetta: foglio, riga d'intestazione, colonne
  // una per una — dieci campi compilati guardando il file — e si tornava
  // indietro con niente. Adesso, se c'e' del lavoro da perdere, la domanda si
  // fa qui: una riga, come per «Inizia nuova comparazione», non una finestra.
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
    // ⚠ Il salvataggio PRIMA della richiesta, per la ragione scritta per
    // esteso in `abbinaLaRiga`: qui sotto `loadReview()` rilegge il confronto
    // e SOSTITUISCE quello in pagina, e una quantità scritta meno di 450 ms fa
    // — o rimasta indietro dopo un salvataggio fallito — se ne andrebbe con
    // lui, cioè un ordine sbagliato senza che niente lo dica (6 settembre
    // 2026). Con lo stato pulito `saveState()` torna vero subito.
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
        // Si manda sempre, anche vuoto, per la stessa ragione della riga qui
        // sotto: il servizio deve poter distinguere «la colonna c'è ma non mi
        // hai detto quali valori valgono» — che rifiuta — da una mappatura in
        // cui la disponibilità non c'entra (6 settembre 2026).
        availableValues: String(valore.availableValues || ""),
        // Si manda sempre, anche vuoto: è la dichiarazione che senza colonna
        // non produce niente e con la colonna dice come sono scritte. Senza
        // questo campo il servizio non guarderebbe nemmeno la colonna scelta,
        // e «dove scrive le sue offerte» sarebbe una domanda senza effetto.
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
  // ⚠ E nemmeno due volte mentre la prima richiesta e' in volo. Il doppio clic
  // — che su questo pulsante e' il gesto piu' naturale del mondo, perche' non
  // succede niente per un secondo — mandava due richieste: il servizio avviava
  // la prima e rispondeva 409 alla seconda, e la pagina mostrava «Il confronto
  // non e' partito» mentre il confronto stava partendo davvero.
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

// A che punto e' il ricalcolo, detto a chi non vede la barra. Scrive dentro
// `#avanzamento-annuncio`, che sta in `index.html`, e' fermo e non viene mai
// distrutto da `render()`: e' l'unico modo in cui `aria-live` funziona davvero
// — un elemento che resta e un testo che cambia. Scrivere sempre lo stesso
// testo farebbe ripetere l'annuncio a ogni giro, quindi si scrive solo quando
// cambia.
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
    // Un controllo perso non è un ricalcolo perso: la catena gira nel servizio
    // locale, non nella pagina. Si riprova al giro dopo — ma se i controlli
    // persi diventano molti, la barra resta ferma all'ultima percentuale per
    // sempre e l'utente aspetta un ricalcolo di cui nessuno sa più niente.
    // Dopo la soglia si dichiara, e si continua comunque a riprovare.
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
    // Il messaggio dipende dal perché si è fermato: quando è una questione di
    // colonne c'è un modo di ripartire subito, ed è quello che va detto.
    if (schemaMappingRequired(stato)) {
      showToast("Un listino ha colonne che non conosco: indicamele qui sotto e riparto.", "error");
      loadSchemaMapping();
    } else {
      showToast("Il confronto non è riuscito: quello di prima è rimasto al suo posto.", "error");
    }
  }
  render();
}

// --- Le quattro finestre, con un comportamento solo ------------------------
// Sono quattro — la colonna d'ordine, il visualizzatore listini, il catalogo e
// lo spostamento fra fornitori — e tutte e quattro dichiarano
// `aria-modal="true"`. Fino a oggi la promessa era fatta quattro volte e
// mantenuta zero: Esc chiudeva le prime due e non le altre due, solo il
// catalogo portava il fuoco dentro all'apertura, nessuna lo restituiva al
// comando che l'aveva aperta, nessuna si chiudeva cliccando sullo sfondo.
//
// L'ordine dell'elenco è quello del markup, dalla finestra più in alto alla più
// in basso: `render()` aggiunge la colonna d'ordine DOPO il contenuto di
// qualunque pagina, quindi è sempre l'ultima disegnata e la prima a chiudersi.
//
// `primiCampi` è una cascata, non un selettore: una finestra che sta ancora
// caricando non ha il proprio campo, e in quel caso il fuoco va sul suo «×» —
// che è comunque dentro la finestra, non dietro.
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

// Chi ha aperto la finestra, per rimetterci il fuoco quando si chiude. Si
// registra la `data-focus-key` del comando, non il nodo: il nodo lo distrugge
// il primo `render()`.
function ricordaChiApreLaFinestra() {
  state.fuocoPrimaDellaFinestra = String(document.activeElement?.dataset?.focusKey || "");
}

// ⚠ `setTimeout(…, 0)`: l'attributo `autofocus` non viene onorato sui nodi
// inseriti con `innerHTML`, ed è esattamente il motivo per cui il catalogo
// faceva già così nonostante l'`autofocus` scritto nel suo markup.
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

// Alla chiusura il fuoco torna dov'era. Se il comando che aveva aperto la
// finestra nel frattempo non c'è più — «Sfoglia il listino» dopo che
// l'abbinamento a mano ha cambiato la scheda del prodotto — si ripiega su
// `#workspace`, che è dove `goToStep()` porta già il fuoco a ogni cambio pagina.
function restituisciIlFuoco() {
  const chiave = state.fuocoPrimaDellaFinestra;
  if (chiave === null) return;
  state.fuocoPrimaDellaFinestra = null;
  window.setTimeout(() => {
    const comando = chiave ? document.querySelector(`[data-focus-key="${CSS.escape(chiave)}"]`) : null;
    comando?.focus({ preventScroll: true });
    // ⚠ «Esiste nel DOM» non vuol dire «puo' prendere il fuoco». Il comando puo'
    // stare dentro un `<details>` chiuso — «Sfoglia il listino X» sta dentro il
    // riquadro del confronto — e allora `querySelector` lo trova, `focus()` non
    // fa niente e il fuoco resta su `<body>`: il Tab riparte dall'inizio della
    // pagina, cioe' il difetto che questa funzione esiste per chiudere. Si
    // guarda dove il fuoco e' finito davvero, non dove lo si e' mandato.
    if (comando && document.activeElement === comando) return;
    document.querySelector("#workspace")?.focus({ preventScroll: true });
  }, 0);
}

// Esc e il clic sullo sfondo passano tutti e due di qui.
function chiudiLaFinestraInCima() {
  const finestra = finestraInCima();
  if (!finestra) return false;
  // ⚠ Il ritorno del fuoco NON sta qui ma dentro le quattro funzioni di
  // chiusura. Da qui passano Esc, il «×» e il clic sullo sfondo; le tre azioni
  // primarie — salvare la colonna d'ordine, aggiungere un prodotto dal
  // catalogo, eseguire lo spostamento — chiudono per conto loro, e chi le usa
  // con la tastiera perdeva il segno esattamente come prima della correzione.
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
      // ⚠ PRIMA il salvataggio, poi la richiesta. Qui sotto il confronto viene
      // riletto dal servizio e SOSTITUISCE quello in pagina: una quantità
      // scritta meno di 450 ms fa — o rimasta indietro perché un salvataggio è
      // fallito e sta per riprovare — non è ancora sul disco, e quella rilettura
      // se la porta via. Il salvataggio successivo consolida il numero vecchio,
      // cioè un ordine sbagliato, senza che niente lo dica (6 settembre 2026).
      // Con lo stato pulito `saveState()` torna vero subito e non costa niente.
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
  // La stessa cosa che tiene la fascia, ma dove non scade: la fascia è una
  // sola e la prossima esclusione la sostituisce.
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
  // ⚠ Solo se la fascia parla di QUESTO prodotto: rimetterne uno faceva sparire
  // l'annullo di un altro appena escluso, e quello e' il lavoro di qualcun
  // altro.
  if (state.exclusionUndo && state.exclusionUndo.id === product.id) state.exclusionUndo = null;
  invalidateCompilationAcceptance();
  scheduleSave();
  render();
}

// Chiede al servizio locale quanto costerebbe spostare tutti i prodotti di un
// fornitore. È solo un preventivo: finché l'utente non conferma non cambia
// niente, e nessun prezzo viene calcolato qui.
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
    // Nel frattempo la finestra può essere stata chiusa o riaperta su un altro
    // fornitore: una risposta in ritardo non deve sovrascrivere quella giusta.
    if (state.supplierMove.from !== requested) return;
    const preview = normalizeMovePreview(payload);
    state.supplierMove.preview = preview;
    // Predefinita: la migliore alternativa per ciascun prodotto.
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

// Applica la scelta: cambia SOLO il fornitore dei prodotti indicati dal
// preventivo. Quantità, esclusioni e origine della quantità restano come sono; le
// conferme no, perché una conferma data su un'offerta non vale per un'altra.
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
    // Stessi colli ma pezzi per collo diversi: la scheda va evidenziata come nel
    // cambio di fornitore singolo.
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
  // Se il riepilogo era filtrato proprio sul fornitore appena svuotato, la sua
  // scheda non esiste più: si torna a mostrare tutti i fornitori, altrimenti
  // resterebbe una pagina vuota senza spiegazione.
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

// Ripristina esattamente le assegnazioni e le conferme di prima dello
// spostamento, sullo stile dell'annulla dell'esclusione.
function undoSupplierMove() {
  const undo = state.supplierMoveUndo;
  if (!undo) return;
  for (const entry of undo.previous) {
    const product = findProduct(entry.id);
    if (!product) continue;
    product.selectedSupplierId = entry.selectedSupplierId;
    product.confirmed = entry.confirmed;
    // Gli avvisi sui pezzi per collo riguardavano lo spostamento annullato.
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
      // La compilazione riscrive tutto lo stato, e senza la versione nuova lo
      // `scheduleSave()` qui sotto verrebbe rifiutato: la raccoglie
      // `requestJson` per tutti, non serve farlo qui.
      state.compileResult = result;
    }
    state.currentStep = 3;
    scheduleSave();
    // La compilazione appena fatta è la prima voce dello storico: senza questa
    // richiesta il riquadro «Compilazioni precedenti» resterebbe indietro di una
    // e sembrerebbe che la cartella nuova non sia stata creata.
    loadCompilazioni();
    // ⚠ E il pannello si apre. `state.compilazioni.aperta` nasce `false`, quindi
    // il riquadro che dice DOVE sono finiti i documenti appena creati restava
    // chiuso, a un clic di distanza e senza che niente lo suggerisse. Subito
    // dopo una compilazione riuscita quella e' l'informazione che si cerca.
    state.compilazioni.aperta = true;
    showToast(state.compileResult.message || "Listini compilati correttamente.");
  } catch (error) {
    // Le scelte restano in state.review e sono già salvate: il pulsante
    // «COMPILA I LISTINI» qui sotto è davvero un modo di riprovare, e la frase
    // può prometterlo. Il testo tecnico non sparisce, va nel pieghevole.
    state.compileFailure = {
      message: "I listini non sono stati preparati. Le tue scelte sono salvate: puoi riprovare col pulsante qui sotto.",
      detail: String(error.message || ""),
      controlli: asArray(error.dettagli?.errors).map(normalizeIssue).filter((voce) => voce.productName || voce.productId),
    };
    // state.runtimeError non si tocca: se il salvataggio era fallito quella
    // frase è ancora vera e sovrascriverla la farebbe sparire.
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
  // Si rileggono a ogni apertura, non una volta sola: una dichiarazione può
  // essere nata in questa stessa sessione, abbinando a mano una riga di
  // listino, e un elenco fermo alla prima apertura non la mostrerebbe.
  caricaLeUguaglianze({ query: "" });
}

function closeSettings() {
  state.impostazioni.aperta = false;
  // La chiave incollata e non salvata non sopravvive all'uscita dalla pagina:
  // restarne in memoria non servirebbe a niente e sarebbe solo un valore in giro.
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
    // ok === false qui non è un guasto della pagina: l'elenco è una comodità e
    // l'identificativo si scrive comunque a mano.
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
    // Azzerata in tutti i rami: dopo l'invio il valore non ha più ragione di
    // restare nella pagina.
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
    // La chiave incollata **non** viene azzerata qui: si prova prima di salvare,
    // e chi ha appena visto "la chiave funziona" deve poter premere "Salva"
    // senza reincollarla.
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

// Un numero scritto in un campo numerico arriva come stringa. Il servizio
// locale rifiuta "3" per un tetto che vale 3.0, ed è giusto che sia severo:
// qui si converte prima di spedire, e quello che non è un numero parte com'è,
// perché il messaggio che spiega il separatore decimale lo scrive il servizio.
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
    // Il messaggio del servizio locale è già in italiano e già spiegato — dice
    // per esempio che i decimali si scrivono con il punto: arriva così com'è.
    state.impostazioni.errore = `Impostazioni non salvate. ${error.message}`;
    showToast("Impostazioni non salvate.", "error");
  } finally {
    state.impostazioni.salvando = false;
    render();
  }
}

stepperElement.addEventListener("click", (event) => {
  // La quarta voce — «Impostazioni», che compare solo mentre sono aperte — non
  // porta `data-step` perche' non e' un passo del lavoro: premerla le chiude e
  // riporta dov'eri.
  if (event.target.closest('[data-action="chiudi-impostazioni"]')) {
    closeSettings();
    return;
  }
  const button = event.target.closest("[data-step]");
  if (!button || !state.review || state.compiling) return;
  goToStep(button.dataset.step);
});

appElement.addEventListener("click", (event) => {
  // ⚠ Lo sfondo scuro chiude la finestra, ma solo se il clic è arrivato
  // PROPRIO lì. Non passa da `data-action` di proposito: `closest()` risalirebbe
  // dal punto cliccato fino allo sfondo, quindi un clic dentro la finestra che
  // finisce su un margine — fra due campi, accanto a un titolo — la chiuderebbe
  // e butterebbe via quello che si stava facendo. Nella finestra della colonna
  // d'ordine vorrebbe dire perdere la scelta appena fatta.
  if (event.target?.classList?.contains?.("dialog-backdrop") && chiudiLaFinestraInCima()) return;
  const target = event.target.closest("[data-action]");
  if (!target) return;
  const action = target.dataset.action;
  // Durante la compilazione non si cambiano i fornitori sotto ai documenti che
  // si stanno creando.
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
  // La fascia «i documenti sono cambiati» compare anche nelle pagine 2 e 3, dove
  // il pulsante del confronto non c'è: questo comando riporta dov'è.
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
    // ⚠ Con le scelte di prima, non a mani vuote: «Rimetti nell'ordine»
    // rimetteva il prodotto con la quantità a zero, cioè lo rimetteva
    // nell'elenco e fuori dall'ordine. Se non ce ne sono — prodotto escluso
    // prima di questa correzione, o memoria del browser ripulita — si torna al
    // comportamento di prima, che è il meglio che si possa fare senza
    // inventare una quantità.
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
      // Prima di azzerare: il prodotto sparisce da questa pagina nello stesso
      // istante, perché `orderedProducts()` tiene solo chi ha quantità maggiore
      // di zero. Senza questo, per rimediare bisognava accorgersene, tornare
      // alla pagina 2, ritrovare il prodotto fra cinquecento e ridigitare una
      // quantità che nel frattempo non si ricorda.
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
      // ⚠ Anche l'esclusione. Fra il «×» e l'annullo l'utente puo' essere
      // passato dalla pagina 2 e aver premuto «Escludi dall'ordine» sullo stesso
      // prodotto: rimettere la sola quantita' produrrebbe un prodotto insieme
      // «Escluso» e con sei colli, contato nei subtotali e nel totale della
      // pagina 3 e poi azzerato dal servizio locale. Il pulsante dice «Rimetti
      // nell'ordine», e questo e' quello che vuol dire — la stessa cosa che fa
      // `restoreProduct`.
      state.excludedProductIds.delete(precedente.id);
      persistExcludedProducts();
      product.quantity = precedente.quantity;
      product.quantitySource = precedente.quantitySource;
      // ⚠ Anche la conferma, che il comando azzera insieme alla quantità: senza,
      // il prodotto tornerebbe nell'ordine con la compilazione bloccata e
      // nessuna riga che dica perché.
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
      // ⚠ Torna «gestionale», non «utente»: se tornasse «utente» il pulsante
      // non le vedrebbe più, e premerlo di nuovo non farebbe niente.
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
  // Il comando scritto sull'avviso: applica il filtro che l'avviso nomina e
  // porta dove si risponde. Il filtro si controlla contro `FILTRI_PRODOTTO`
  // prima di scriverlo nello stato: una voce sbagliata lascerebbe la pagina su
  // un elenco vuoto senza dire perche'.
  if (action === "vai-al-filtro") {
    const filtro = String(target.dataset.filtro || "");
    if (FILTRI_PRODOTTO[filtro]) {
      state.filters.status = filtro;
      // La ricerca e il filtro per tipo restringerebbero l'elenco che l'avviso
      // ha appena promesso: si azzerano, come fa «Mostra i prodotti» delle
      // offerte.
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
        // I separatori sono quelli di QUEL foglio: tenerne uno scelto qui
        // dentro descriverebbe una riga che in questo foglio non c'e'.
        valore.dataStartBreak = "";
        valore.markerText = "";
      }
      if (campo === "dataStartBreak") {
        const documento = (state.schemaMapping.data?.documents || []).find((voce) => String(voce.profileId || "") === identificativo);
        schemaApplicaSeparatore(valore, schemaSelectedSheet(documento || {}, valore));
      }
      if (campo === "headerRow" && Number(valore.dataStartRow) <= Number(valore.headerRow)) {
        valore.dataStartRow = Number(valore.headerRow) + 1;
        // La riga dei prodotti l'ha appena decisa l'intestazione: la regola
        // che diceva un'altra riga non descrive piu' questo taglio.
        valore.dataStartBreak = "";
        valore.markerText = "";
      }
    }
    delete state.schemaMapping.results[identificativo];
    state.schemaMapping.error = "";
    // ⚠ `render()` SOSTITUISCE l'HTML: la tendina appena usata muore e ne nasce
    // una nuova, senza fuoco. Chi assegna dodici colonne con la tastiera
    // ricomincia dodici volte dall'inizio della pagina. L'id c'è già e
    // `idCampoSchema` lo costruisce con i due valori che stanno nel markup,
    // quindi è lo stesso prima e dopo il ridisegno: basta ritrovarlo.
    const tornaSu = String(target.id || "");
    render();
    if (tornaSu) document.querySelector(`#${CSS.escape(tornaSu)}`)?.focus();
    return;
  }

  if (target.dataset.elencoModelli !== undefined) {
    // L'elenco riempie il campo di testo, che resta il valore vero: un modello
    // uscito ieri non è nell'elenco e deve poter essere scritto lo stesso.
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
    // Cambia solo la destinazione evidenziata: nessun prodotto si muove finché
    // non si preme "Sposta i prodotti".
    state.supplierMove.choiceId = String(target.value || "");
    rerenderPreservingFocus();
    return;
  }

  // ⚠ `data-product-confirm` non esiste più: la conferma era una casella e dal
  // 22 agosto 2026 è un pulsante, come il no che risponde alla stessa domanda.
  // Il ramo che la leggeva è stato tolto invece di essere lasciato lì a
  // rispondere a un elemento che nessuno disegna.

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
    // ⚠ Le righe del listino di prima escono di scena SUBITO. Restavano in
    // tabella, cliccabili, sotto il nome del fornitore nuovo, per tutto il
    // tempo della richiesta: «È questo» avrebbe mandato quel numero di riga
    // all'altro fornitore, cioè un abbinamento sul listino sbagliato, cioè un
    // ordine sbagliato. Stessa ragione per l'esito: «Abbinato» sopra il listino
    // di un altro si legge come una cosa che non è successa.
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
    // Toccare i filtri a mano riprende il controllo dall'azione "Mostra i
    // prodotti" del pannello offerte. L'ordinamento no: non è un filtro, e
    // cambiare l'ordine non deve far sparire l'elenco che si sta guardando.
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
    // La chiave resta soltanto in memoria: qui non si ridisegna niente, così il
    // valore non passa mai per l'HTML della pagina. Si aggiorna a mano il solo
    // pulsante che dipende da lei.
    state.impostazioni.nuovaChiave = target.value;
    const salva = document.querySelector('[data-action="salva-chiave"]');
    if (salva) salva.disabled = state.impostazioni.salvandoChiave || state.impostazioni.provando || !state.impostazioni.nuovaChiave;
    return;
  }
  if (target.dataset.impostazione) {
    state.impostazioni.valori[target.dataset.impostazione] = target.value;
    // Solo il modello ha qualcosa da ridisegnare mentre si scrive: il prezzo e
    // l'avviso "non è nell'elenco". I limiti no, e ridisegnarli farebbe
    // saltare il cursore a ogni tasto.
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
    // Il render è rimandato di 180 ms: gli avvisi vanno invalidati subito, altrimenti
    // una lettura in quella finestra userebbe il calcolo precedente alla digitazione.
    invalidateIssues();
    invalidateCompilationAcceptance();
    scheduleSave();
    window.clearTimeout(state.quantityRenderTimer);
    state.quantityRenderTimer = window.setTimeout(() => rerenderPreservingFocus(), 180);
    return;
  }
  if (target.matches("[data-promotion-search]")) {
    // Il riquadro delle offerte resta aperto mentre si scrive: si ridisegna
    // conservando il fuoco, come per la ricerca dei prodotti.
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
    // Il listino è di ottomila righe e sta sul servizio locale: si aspetta che
    // si smetta di scrivere, invece di chiederlo a ogni tasto.
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

// Una quantità d'ordine non deve poter cambiare senza un gesto esplicito. Nei
// programmi di navigazione che lo fanno ancora (Firefox, Chromium prima della
// versione 119) la rotellina sopra un campo numerico a fuoco lo incrementa da
// sola, una tacca per scatto, mentre la pagina resta ferma. Si toglie il fuoco e
// si annulla l'evento: la scorsa successiva scorre la pagina normalmente.
appElement.addEventListener("wheel", (event) => {
  const field = event.target.closest?.('input[type="number"]');
  if (!field || document.activeElement !== field) return;
  event.preventDefault();
  field.blur();
}, { passive: false });

// L'evento "toggle" di <details> non risale, ma la fase di cattura lo vede lo
// stesso: serve per ricordare che il riquadro delle offerte era aperto.
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
  // Esc chiude la finestra in cima, qualunque delle quattro sia. Prima ne
  // chiudeva due: il visualizzatore listini e la finestra della colonna
  // d'ordine si chiudevano soltanto col «×» — e il visualizzatore è la via
  // d'uscita quando l'abbinamento automatico non ce l'ha fatta, cioè si apre
  // quando qualcosa è già andato storto.
  if (event.key === "Escape") chiudiLaFinestraInCima();
});

window.addEventListener("beforeunload", (event) => {
  if (mode === "live" && state.dirty) {
    event.preventDefault();
    event.returnValue = "";
  }
});

loadReview();
// Chi ricarica la pagina mentre la catena lavora deve ritrovare la barra dov'era:
// il ricalcolo gira nel servizio locale e non si ferma perché il browser si è
// chiuso. Un errore qui non conta niente — vuol dire solo che non c'è nessun
// ricalcolo da riprendere.
//
// ⚠ E deve ritrovare anche l'esito, non solo la barra. Prima qui si riprendeva
// il solo IN_CORSO: un ricalcolo fermato a metà, o finito con degli avvisi,
// spariva dalla pagina al primo ricaricamento — e gli avvisi di quel ricalcolo
// sono esattamente le frasi che dicono che la compilazione va ricontrollata.
// Il servizio lo stato finale ce l'ha (lo tiene in `pipeline_status.json` e lo
// rilegge anche dopo un riavvio): era la pagina a buttarlo via.
if (mode === "live") {
  requestJson(API.pipelineStato)
    .then((stato) => {
      const esito = String(stato?.stato || "");
      // ⚠ Qui c'era scritto che IN_ATTESA «vuol dire che non è mai partito
      // niente». Non è più vero: IN_ATTESA con `cambiamento` vuol dire che i
      // documenti sono cambiati dopo l'ultimo confronto, e quella fascia deve
      // tornare anche dopo un ricaricamento della pagina — è proprio quando si
      // riapre il programma il lunedì dopo che serve sapere che i prezzi a
      // schermo sono vecchi. Senza `cambiamento` l'assunto originale resta
      // giusto: nessun ricalcolo da riprendere, niente da mostrare.
      if (!esito || (esito === "IN_ATTESA" && !cambiamentoDocumenti(stato))) return;
      state.pipeline.stato = stato;
      state.pipeline.chiesto = true;
      render();
      if (schemaMappingRequired(stato)) loadSchemaMapping();
      // Si continua a interrogare solo una run viva: su una finita il timer
      // ripeterebbe per sempre la stessa risposta.
      if (esito === "IN_CORSO") pianificaControlloPipeline();
    })
    .catch(() => {});
}

// ---------------------------------------------------------------------------
// La data della versione, in alto a destra.
//
// Questo PC si allinea a GitHub da solo a ogni avvio, e quando non ci riesce —
// credenziali scadute, rete che non risponde — per scelta non blocca niente:
// lo scrive in una riga di console e parte con quello che ha. Quella riga non
// la legge nessuno, e un programma fermo da mesi ha esattamente lo stesso
// aspetto di uno aggiornato stamattina. Con la data scritta qui basta
// guardarla, o farsela leggere al telefono, per sapere quale dei due e'.
//
// Sta nella topbar, che e' fuori da `#app`: `render()` sostituisce l'HTML del
// solo `#app`, quindi questa riga si scrive una volta e nessun ridisegno se la
// porta via.
if (mode === "live") {
  const elementoVersione = document.querySelector("#versione-pubblicata");
  if (elementoVersione) {
    requestJson(API.salute)
      .then((salute) => {
        const quando = formatDayMonthYear(salute?.versionePubblicata);
        if (quando) {
          const frase = `versione del ${quando}`;
          elementoVersione.textContent = frase;
          // Data e ora precise per chi sta cercando di capire un difetto: in
          // pagina basta il giorno, nel dettaglio serve sapere quale avvio.
          elementoVersione.title = formatDateTime(salute.versionePubblicata);
          // Senza questo chi legge con la sintesi vocale sentirebbe il
          // tooltip: `title` da solo diventa il nome dell'elemento e copre il
          // testo scritto.
          elementoVersione.setAttribute("aria-label", frase);
        } else {
          // Nessuna data vuol dire che qui git non c'e' o non risponde: e' una
          // risposta, non un guasto, e va detta invece di lasciare il vuoto.
          elementoVersione.textContent = "versione sconosciuta";
          elementoVersione.title = "Non riesco a leggere quale versione sta girando su questo computer.";
        }
      })
      .catch(() => {});
  }
}
