#!/usr/bin/env node
import crypto from "node:crypto";
import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";

import { ContenitoreNonLeggibile, FoglioNonScrivibile, apriLibro } from "./lib/xlsx_in_posizione.mjs";

// Ogni errore esce di qui marcato cosi', su una riga sola.  Serve a chi ci
// chiama (`app/server.py`): senza la marca, quello che arrivava all'utente era
// la traccia di Node con i percorsi assoluti, invece della frase italiana che
// il writer aveva gia' pronta.
const MARCA_ERRORE = "ERRORE_COMPILAZIONE: ";

// E il riepilogo di fine lavoro esce marcato cosi', su una riga sola, in JSON
// compatto: i conteggi e gli avvisi della compilazione servono a chi ci chiama,
// che cerca la riga marcata e non legge tutto lo `stdout` (vedi in fondo).
const MARCA_RIEPILOGO = "RIEPILOGO_COMPILAZIONE: ";

/**
 * Un guasto che il writer sa spiegare, con la frase gia' scritta per l'utente.
 *
 * ⚠ La marca `ERRORE_COMPILAZIONE:` va SOLO sulle frasi di questa classe.  La
 * prima versione marcava qualunque messaggio uscisse dal `catch` finale, e in
 * pagina arrivavano gli errori degli altri — `ENOENT ... C:\...` con i percorsi
 * del computer, le chiavi di risorsa .NET della libreria
 * (`Arg_ArgumentOutOfRangeException`) — vestiti da frase italiana.  Quello che
 * il writer non ha scritto lui non e' una spiegazione: e' un dettaglio tecnico,
 * e sta sulla console, non in pagina.
 */
class ErroreCompilazione extends Error {}

const FRASE_GUASTO_IMPREVISTO =
  "la creazione delle copie dei listini si è fermata per un guasto imprevisto; " +
  "i dettagli tecnici sono sulla console del programma. " +
  "Il piano ordini è completo e si può scaricare.";

// ⚠ Fino al 5 settembre 2026 qui si caricava `@oai/artifact-tool`, una
// libreria .NET in WebAssembly da 24 MB che importava il listino, se lo
// ricostruiva in memoria e lo riscriveva da capo — perdendo per strada celle,
// titoli di sezione e la forma che Excel si aspetta.  Adesso le copie le fa
// `lib/xlsx_in_posizione.mjs`, che tocca il solo XML del foglio dentro lo ZIP e
// copia ogni altra parte byte per byte: nessuna libreria, nessun runtime.

const SUPPLIER_NAMES = {
  betulla: "BETULLA",
  cipresso: "CIPRESSO",
  larice: "LARICE",
  noce: "NOCE",
};

/**
 * I nomi leggibili dichiarati dalla configurazione (`display_name`).
 *
 * ⚠ Fino al 14 agosto 2026 la tabella qui sopra era tutto quello che il writer
 * sapeva: per un fornitore nuovo l'utente si trovava scritto `NUOVO_FORNITORE`,
 * con l'underscore, dentro le frasi **e dentro il nome del file d'ordine** che
 * finisce al fornitore.  Come si chiama un fornitore e' un dato del registro,
 * non del codice: arriva con la regola di scrittura, e la tabella cablata resta
 * solo come ripiego per le configurazioni che il nome non lo dichiarano.
 *
 * La mappa si riempie una volta sola, appena letta la configurazione, perche'
 * `supplierName` lo chiamano una trentina di frasi sparse nel file — alcune
 * prima ancora che la regola di quel fornitore sia stata controllata.
 */
const nomiDichiarati = new Map();

function registraNomiDichiarati(rules) {
  for (const [supplier, rule] of Object.entries(rules)) {
    if (!rule || typeof rule !== "object" || Array.isArray(rule)) continue;
    const dichiarato = String(rule.display_name ?? rule.displayName ?? "").trim();
    if (dichiarato) nomiDichiarati.set(supplier, dichiarato);
  }
}

function argsOf(argv) {
  const result = {};
  for (let index = 2; index < argv.length; index += 2) {
    const key = String(argv[index] || "");
    const value = argv[index + 1];
    if (!key.startsWith("--") || value === undefined) {
      throw new ErroreCompilazione(`Argomento non valido: ${key || "mancante"}`);
    }
    result[key.slice(2)] = value;
  }
  for (const required of ["plan", "output-dir", "config"]) {
    if (!result[required]) throw new ErroreCompilazione(`Argomento obbligatorio: --${required}`);
  }
  return result;
}

function supplierName(supplier) {
  return nomiDichiarati.get(supplier) || SUPPLIER_NAMES[supplier] || supplier.toUpperCase();
}

/** «CAFFÈ» e «CAFFE» sono la stessa parola: l'accento si stacca e si butta. */
function senzaAccenti(testo) {
  return String(testo ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

/**
 * Il nome del fornitore ridotto a quello che un nome di file regge.
 *
 * ⚠ Nelle frasi il nome va scritto come l'ha dichiarato il registro — «Sapori &
 * Co. S.r.l.» — ma lo stesso nome finisce dentro `ORDINE_<nome>_<listino>.xlsx`,
 * e Windows nei nomi di file i due punti, la barra, l'asterisco e il punto
 * interrogativo non li accetta: il file non si scriverebbe affatto e il
 * fornitore non riceverebbe l'ordine.  Qui si tiene solo quello che passa
 * ovunque; la frase per l'utente non si tocca.
 */
function nomePerIlFile(supplier) {
  const ripulito = senzaAccenti(supplierName(supplier))
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  // Un nome fatto solo di segni non lascerebbe niente: meglio una parola
  // qualunque che un file chiamato `ORDINE__listino.xlsx`.
  return ripulito || "FORNITORE";
}

function copyDestination(originalPath, supplier, outputDir) {
  const base = path.parse(path.resolve(originalPath)).name;
  return path.join(outputDir, `ORDINE_${nomePerIlFile(supplier)}_${base}.xlsx`);
}

function normalizeSupplierId(value) {
  return String(value || "").trim().toLowerCase();
}

function normalizeColumn(value, supplier, quale = "ordine") {
  const column = String(value || "").trim().toUpperCase();
  if (!/^[A-Z]{1,3}$/.test(column)) {
    throw new ErroreCompilazione(`Colonna ${quale} non valida per ${supplierName(supplier)}.`);
  }
  return column;
}

/**
 * La colonna da controllare, dichiarata come lettera («A», «AB») o come numero
 * 1-based: sono i due modi in cui i fogli di calcolo la scrivono, e il registro
 * puo' portare l'uno o l'altro.  Torna sempre il numero, perche' e' cosi' che
 * si pesca dentro la griglia gia' letta.
 */
function colonnaDaControllare(value, quale, supplier) {
  if (value === undefined || value === null || String(value).trim() === "") return null;
  const testo = String(value).trim();
  if (/^\d+$/.test(testo)) {
    const numero = Number(testo);
    if (!Number.isInteger(numero) || numero < 1) {
      throw new ErroreCompilazione(`Colonna ${quale} non valida per ${supplierName(supplier)}.`);
    }
    return numero;
  }
  return numeroDiColonna(normalizeColumn(testo, supplier, quale));
}

/**
 * Le colonne del listino da confrontare con il piano prima di scrivere.
 *
 * Tutto facoltativo e indipendente l'uno dall'altro: `ean_column` e
 * `description_column` possono esserci tutte e due, una sola o nessuna.  Senza
 * `verify`, o con un `verify` vuoto, si scrive come si e' sempre scritto: una
 * configurazione vecchia non deve smettere di funzionare per una chiave nuova.
 */
function normalizeVerify(rule, supplier) {
  const verify = rule.verify ?? rule.verifica ?? null;
  if (!verify || typeof verify !== "object" || Array.isArray(verify)) {
    return { eanColumn: null, descriptionColumn: null };
  }
  return {
    eanColumn: colonnaDaControllare(verify.ean_column ?? verify.eanColumn, "EAN", supplier),
    descriptionColumn: colonnaDaControllare(
      verify.description_column ?? verify.descriptionColumn, "descrizione", supplier,
    ),
  };
}

/** `A` diventa 1, `D` diventa 4: il numero di colonna come lo conta Excel. */
function numeroDiColonna(lettera) {
  let numero = 0;
  for (const carattere of String(lettera).toUpperCase()) {
    numero = numero * 26 + (carattere.charCodeAt(0) - 64);
  }
  return numero;
}

function normalizeHeader(value) {
  return String(value ?? "")
    .replace(/\u00a0/g, " ")
    .trim()
    .replace(/\s+/g, " ")
    .toUpperCase();
}

function positiveInteger(value, label) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new ErroreCompilazione(`${label} non valido.`);
  }
  return parsed;
}

async function loadWriterConfig(argv) {
  if (!argv.config) {
    // Il ripiego cablato («betulla in C, larice in D») e' stato tolto apposta:
    // le regole vengono dal registro attraverso la configurazione, e un
    // writer che conosce le colonne per conto suo puo' scrivere l'ordine in
    // una colonna diversa da quella dichiarata (revisione avversariale del
    // 13 agosto 2026).
    throw new ErroreCompilazione(
      "Serve la configurazione di scrittura (--config): le regole vengono dal registro, non dal codice.",
    );
  }
  const configPath = path.resolve(argv.config);
  let config;
  try {
    config = JSON.parse(await fs.readFile(configPath, "utf8"));
  } catch (error) {
    // Il dettaglio (un ENOENT, una sintassi JSON) porta percorsi assoluti e
    // inglese: va sulla console come `cause`, non nella frase per l'utente.
    throw new ErroreCompilazione("Configurazione di scrittura non leggibile.", { cause: error });
  }
  if (!config || typeof config !== "object" || Array.isArray(config)) {
    throw new ErroreCompilazione("Configurazione di scrittura non valida.");
  }
  return config;
}

function normalizedObjectMap(value, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new ErroreCompilazione(`${label} mancante o non valido.`);
  }
  const result = {};
  for (const [key, item] of Object.entries(value)) {
    const supplier = normalizeSupplierId(key);
    if (supplier) result[supplier] = item;
  }
  return result;
}

function configuredRule(rules, supplier) {
  const supplied = rules[supplier];
  if (!supplied || typeof supplied !== "object" || Array.isArray(supplied)) {
    throw new ErroreCompilazione(`Manca la regola di scrittura verificata per ${supplierName(supplier)}.`);
  }
  return supplied;
}

/**
 * Il listino di questo fornitore lo scrive il servizio locale, non questo writer.
 *
 * ⚠ Fino al 18 agosto 2026 la domanda era `supplier === "noce"`, scritta in
 * tre punti di questo file. Il nome pero' non c'entra: quello che decide e' la
 * procedura di scrittura che la configurazione **dichiara** — la stessa che
 * `app/server.py` legge con `procedura_di_scrittura`, e che il registro porta
 * fin qui come `compilazione`. Con il nome cablato, un fornitore nuovo che
 * mandasse un `.xls` sarebbe finito in questo writer e sarebbe stato fermato
 * accusando il suo documento; e il giorno in cui Noce mandasse un `.xlsx`
 * come tutti, il nome lo avrebbe mandato lo stesso alla patch in posizione.
 *
 * Chi non dichiara niente passa di qui: e' la strada normale, quella di tutti
 * i listini `.xlsx`.
 */
const PATCH_IN_POSIZIONE = "patch_xls_in_posizione";

function scrittoAltrove(rules, supplier) {
  const supplied = rules?.[supplier];
  if (!supplied || typeof supplied !== "object" || Array.isArray(supplied)) return false;
  return String(supplied.compilazione || "").trim() === PATCH_IN_POSIZIONE;
}

function normalizeRule(rule, supplier) {
  const sheet = String(rule.sheet_name || rule.sheet || "").trim();
  const orderColumn = normalizeColumn(rule.order_column || rule.orderColumn, supplier);
  const dataStartValue = rule.data_start_row ?? rule.dataStartRow ?? 2;
  const dataStartRow = positiveInteger(dataStartValue, `Riga iniziale ${supplierName(supplier)}`);
  const headerRow = rule.header_row ?? rule.headerRow;
  const expectedHeader = rule.expected_header ?? rule.expectedHeader ?? rule.order_header ?? rule.orderHeader;
  const sourceSha256 = String(rule.source_sha256 ?? rule.sourceSha256 ?? "").trim().toLowerCase();

  if (!sheet) {
    throw new ErroreCompilazione(`Manca il foglio verificato per ${supplierName(supplier)}.`);
  }
  // ⚠ Qui c'era un blocco `if (supplier === "cipresso")` che pretendeva colonna
  // «G» e intestazione «ORDINE» scritte nel codice.  Il 14 agosto 2026 ha
  // impedito OGNI compilazione — anche di LARICE e NOCE, perche' basta una
  // regola rifiutata a fermare tutto — su un listino CIPRESSO che quella
  // settimana la colonna G ce l'aveva vuota, cosa che l'utente aveva
  // **confermato** nella mappatura guidata e che il registro dichiarava.
  // Un nome di fornitore nel codice contro una regola dichiarata nel registro:
  // e' esattamente il contrario del vincolo su cui poggia il programma.
  // Al suo posto non c'e' una regola nuova, c'e' la regola che c'era gia': si
  // verifica **quello che la configurazione dichiara**.  Se dice che testo deve
  // esserci nella cella dell'intestazione, si controlla che ci sia; se dice che
  // l'utente ha confermato che quella cella e' vuota, si controlla che sia
  // ancora vuota; se non dichiara nessuna intestazione — il listino LARICE una
  // riga di intestazione non ce l'ha proprio — non c'e' niente da verificare, e
  // a difendere la scrittura restano foglio, colonna, riga d'inizio, impronta
  // del file e il controllo di EAN e descrizione sulla riga di destinazione.
  const blankHeaderConfirmed = rule.blank_header_confirmed === true || rule.blankHeaderConfirmed === true;

  return {
    sheet,
    orderColumn,
    dataStartRow,
    headerRow: headerRow === undefined || headerRow === null ? null : positiveInteger(headerRow, `Riga intestazione ${supplierName(supplier)}`),
    expectedHeader: expectedHeader === undefined || expectedHeader === null ? "" : String(expectedHeader),
    blankHeaderConfirmed,
    sourceSha256,
    verify: normalizeVerify(rule, supplier),
  };
}

async function sha256File(filePath) {
  const hash = crypto.createHash("sha256");
  for await (const chunk of createReadStream(filePath)) hash.update(chunk);
  return hash.digest("hex");
}

async function verifySourceHash(source, rule, supplier) {
  if (!rule.sourceSha256) return;
  const observed = await sha256File(source);
  if (observed !== rule.sourceSha256) {
    throw new ErroreCompilazione(
      `Il listino ${supplierName(supplier)} è cambiato dopo la verifica: non creo copie ` +
        "finché non viene ricontrollato.",
    );
  }
}

function worksheetForRule(libro, rule, supplier) {
  const foglio = libro.foglio(rule.sheet);
  if (!foglio) {
    throw new ErroreCompilazione(`Foglio verificato non trovato per ${supplierName(supplier)}: ${rule.sheet}.`);
  }
  return foglio;
}

function verifyOrderHeader(sheet, rule, supplier) {
  if (!rule.headerRow) return;
  const observed = sheet.valore(`${rule.orderColumn}${rule.headerRow}`);
  // La conferma «quella cella e' vuota» e' una verifica come le altre, e va
  // rifatta qui sul documento aperto: fra la mappatura confermata e la
  // compilazione il fornitore puo' aver messo un'intestazione dove non c'era,
  // e scrivere sotto un titolo che nessuno ha letto e' il caso da cui tutte
  // queste guardie sono nate.
  if (rule.blankHeaderConfirmed && !rule.expectedHeader) {
    if (normalizeHeader(observed) !== "") {
      throw new ErroreCompilazione(
        `Intestazione non verificata per ${supplierName(supplier)} in ${rule.orderColumn}${rule.headerRow}: `
        + `la mappatura confermata dice che quella cella è vuota, invece contiene «${String(observed)}».`,
      );
    }
    return;
  }
  if (!rule.expectedHeader) return;
  if (normalizeHeader(observed) !== normalizeHeader(rule.expectedHeader)) {
    throw new ErroreCompilazione(
      `Intestazione non verificata per ${supplierName(supplier)} in ${rule.orderColumn}${rule.headerRow}: atteso ${rule.expectedHeader}.`,
    );
  }
}

function orderRowsForSupplier(orders, supplier) {
  const quantities = new Map();
  for (const order of orders) {
    const row = Number(order.supplier_source_row);
    const quantity = Number(order.quantity);
    if (!Number.isInteger(row) || row < 1) {
      throw new ErroreCompilazione(`Riga ${supplierName(supplier)} non valida: ${order.supplier_source_row}.`);
    }
    if (!Number.isInteger(quantity) || quantity < 1) {
      throw new ErroreCompilazione(`Quantità ${supplierName(supplier)} non valida alla riga ${row}.`);
    }
    quantities.set(row, (quantities.get(row) ?? 0) + quantity);
  }
  return quantities;
}

/** Quello che c'e' scritto in una cella e' una quantita' d'ordine? */
function eUnaQuantita(valore) {
  if (valore === null || valore === undefined || typeof valore === "boolean") return false;
  if (typeof valore === "number") return Number.isFinite(valore);
  const testo = String(valore).trim();
  if (!testo) return false;
  return Number.isFinite(Number(testo.replace(",", ".")));
}

/** Da `[3,4,5,9]` a `[[3,5],[9,9]]`: i blocchi contigui, per pulirli in una volta. */
function blocchiContigui(righe) {
  const blocchi = [];
  for (const riga of righe) {
    const ultimo = blocchi[blocchi.length - 1];
    if (ultimo && riga === ultimo[1] + 1) ultimo[1] = riga;
    else blocchi.push([riga, riga]);
  }
  return blocchi;
}

/**
 * Azzera le quantita' gia' scritte nella colonna d'ordine — **e solo quelle**.
 *
 * ⚠ Fino al 12 agosto 2026 si cancellava tutta la colonna in un colpo solo.  Su
 * LARICE quella colonna non porta quantita': porta **641 titoli di sezione**
 * («DENT. E SPAZZ. AQUAFRISK OPPORTUNITA'» alla riga 963), e la copia
 * consegnabile li perdeva tutti.  Azzerare le quantita' preesistenti resta
 * giusto — senza, si spedirebbero righe fantasma — ma una riga che prodotto non
 * e' non ha una quantita' da azzerare: ha del testo, e il testo resta.
 *
 * La regola e' quella di sempre in questo programma: **una quantita' e' un
 * numero.**  E' la stessa che `app/xls_writer.py` applica al `.xls` di
 * Noce, dove una cella della colonna d'ordine che non e' un numero a
 * lunghezza fissa ferma tutto.
 */
function azzeraQuantitaPreesistenti(sheet, rule, used, usedValues, lastRow) {
  const primaRigaUsata = (used.rowIndex ?? 0) + 1;
  const primaColonnaUsata = (used.columnIndex ?? 0) + 1;
  const scarto = numeroDiColonna(rule.orderColumn) - primaColonnaUsata;
  if (scarto < 0 || scarto >= (used.columnCount ?? 0)) return 0;
  const daAzzerare = [];
  for (let riga = Math.max(rule.dataStartRow, primaRigaUsata); riga <= lastRow; riga += 1) {
    const valori = usedValues[riga - primaRigaUsata];
    if (!valori) continue;
    if (eUnaQuantita(valori[scarto])) daAzzerare.push(riga);
  }
  for (const [primo, ultimo] of blocchiContigui(daAzzerare)) sheet.svuota(rule.orderColumn, primo, ultimo);
  return daAzzerare.length;
}

/** L'EAN come si confronta: `8000000000011.0` e `8000000000011` sono lo stesso codice. */
function normalizzaEan(valore) {
  if (valore === null || valore === undefined || typeof valore === "boolean") return "";
  const testo = String(valore).trim();
  // ⚠ Un EAN e' fatto di cifre, ma i fogli di calcolo lo restituiscono spesso
  // come numero: il piano se lo porta dietro con la coda decimale mentre il
  // listino lo legge intero (o viceversa), e due scritture dello stesso codice
  // non devono sembrare due prodotti diversi.
  return /^\d+\.0*$/.test(testo) ? testo.replace(/\.0*$/, "") : testo;
}

/**
 * La descrizione come si confronta: maiuscole, senza accenti, con qualunque
 * sequenza di caratteri non alfanumerici ridotta a un solo spazio.
 *
 * ⚠ Il confronto e' largo apposta.  La descrizione del piano puo' essere stata
 * ripulita a monte — la data di scadenza tolta, i doppi spazi schiacciati — e
 * confrontarla alla lettera fermerebbe ordini buoni per una virgola.
 */
function normalizzaDescrizione(valore) {
  return senzaAccenti(valore).toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim();
}

/**
 * Che cosa c'e' scritto in `(riga, colonna)` del listino di partenza.
 *
 * Si pesca dalla griglia gia' in mano (`griglia()`), come fa
 * `azzeraQuantitaPreesistenti`: una lettura per cella su un listino da
 * migliaia di righe costa.
 */
function valoreDiCella(used, usedValues, riga, numeroColonna) {
  const primaRigaUsata = (used.rowIndex ?? 0) + 1;
  const primaColonnaUsata = (used.columnIndex ?? 0) + 1;
  const scarto = numeroColonna - primaColonnaUsata;
  if (scarto < 0) return null;
  const valori = usedValues[riga - primaRigaUsata];
  if (!valori || scarto >= valori.length) return null;
  return valori[scarto] ?? null;
}

/** Il numero di colonna scritto come lo scrive un foglio di calcolo: 1 → «A», 28 → «AB». */
function letteraDiColonna(numero) {
  let resto = Number(numero);
  let lettera = "";
  while (resto > 0) {
    const modulo = (resto - 1) % 26;
    lettera = String.fromCharCode(65 + modulo) + lettera;
    resto = Math.floor((resto - modulo) / 26);
  }
  return lettera || String(numero);
}

/**
 * In quale colonna di quella riga sta davvero il codice che il piano cerca.
 *
 * ⚠ Un `ean_column` dichiarato sulla colonna sbagliata produce **lo stesso
 * errore** di un listino cambiato — «quella riga quel prodotto non ce l'ha
 * piu'» — e manda a cercare il guasto nel posto sbagliato: il listino e' a
 * posto, e' la configurazione che indica un'altra colonna.  Se il codice del
 * piano sta li' accanto, in un'altra colonna della stessa riga, e' la
 * configurazione a doverlo dire.
 */
function colonnaDoveStaDavvero(used, usedValues, riga, ean) {
  if (!ean) return null;
  const primaRigaUsata = (used.rowIndex ?? 0) + 1;
  const primaColonnaUsata = (used.columnIndex ?? 0) + 1;
  const valori = usedValues[riga - primaRigaUsata];
  if (!valori) return null;
  for (let scarto = 0; scarto < valori.length; scarto += 1) {
    if (normalizzaEan(valori[scarto]) === ean) return primaColonnaUsata + scarto;
  }
  return null;
}

/**
 * Che la riga di destinazione porti ancora il prodotto che il piano si aspetta.
 *
 * ⚠ Il 12 agosto 2026 un ordine e' finito sulla riga 2600 — olio Carapelli al
 * posto del prodotto atteso.  Per BETULLA, LARICE ed CIPRESSO la quantita' andava
 * in `colonna+riga` alla cieca: lo sha256 del listino difende dal **file**
 * cambiato, non da un `supplier_source_row` sbagliato, e in tutto il writer non
 * c'era una sola occorrenza di EAN.  Questo e' il controllo che l'avrebbe
 * intercettato, e fino a oggi esisteva solo per Noce (`app/xls_writer.py`).
 *
 * Dove si allontana da Noce, e perche':
 *
 * - **Piano senza EAN e listino con l'EAN e' un avviso, non un errore.**  Per
 *   gli espositori LARICE la riga padre l'EAN ce l'ha e il piano no: fallire
 *   chiuso come fa Noce bloccherebbe ordini legittimi.
 * - **La descrizione diversa e' un avviso.**  Quella del piano puo' essere
 *   stata ripulita a monte, e fermarsi li' butterebbe via ordini buoni.
 *
 * L'EAN che non coincide resta invece un errore: quella riga il prodotto del
 * piano non ce l'ha piu', e per quel fornitore non si crea niente.
 *
 * Si guarda **riga per riga d'ordine**, non per riga del listino: `verificate` e
 * `nonVerificabili` sommate fanno le righe che il piano porta per quel
 * fornitore.
 */
function verificaRigheDiDestinazione({ used, usedValues, rule, supplier, orders }) {
  const nome = supplierName(supplier);
  const avvisi = [];
  let verificate = 0;
  let nonVerificabili = 0;

  for (const order of orders) {
    const riga = Number(order.supplier_source_row);
    const eanDelPiano = normalizzaEan(order.supplier_ean);
    const eanDelListino = rule.verify.eanColumn
      ? normalizzaEan(valoreDiCella(used, usedValues, riga, rule.verify.eanColumn))
      : "";

    if (rule.verify.eanColumn && eanDelPiano) {
      // La cella vuota casca qui dentro apposta: se il piano l'EAN ce l'ha e la
      // riga non lo porta piu', quella riga quel prodotto non e'.
      if (eanDelListino !== eanDelPiano) {
        const altrove = colonnaDoveStaDavvero(used, usedValues, riga, eanDelPiano);
        // ⚠ Trovare l'EAN del piano in un'altra colonna NON basta a dire che la
        // colonna dichiarata sia sbagliata: un fornitore che ripete il codice
        // nella colonna dell'articolo lo mette anche su una riga che il
        // prodotto non ce l'ha piu'.  Chi crede alla frase sbagliata sposta
        // `ean_column` e spegne per sempre questa difesa.  Quindi si guarda
        // anche la descrizione, che qui e' l'unica cosa che sa distinguere i
        // due casi, e quando non c'e' si dicono tutte e due le possibilita'.
        const descrizioneDelPiano = String(order.supplier_description ?? "").trim();
        const descrizioneDelListino = rule.verify.descriptionColumn
          ? String(valoreDiCella(used, usedValues, riga, rule.verify.descriptionColumn) ?? "").trim()
          : "";
        const laRigaEQuellaGiusta = Boolean(
          descrizioneDelPiano && descrizioneDelListino &&
            normalizzaDescrizione(descrizioneDelListino) === normalizzaDescrizione(descrizioneDelPiano),
        );
        const laRigaEUnAltraMerce = Boolean(
          descrizioneDelPiano && descrizioneDelListino && !laRigaEQuellaGiusta,
        );
        if (altrove !== null && altrove !== rule.verify.eanColumn && !laRigaEUnAltraMerce) {
          // Ci si ferma lo stesso — su una configurazione che non si sa leggere
          // non si scrive — ma si dice dove guardare, e lo si dice come una
          // certezza solo quando la descrizione conferma che la riga e' quella.
          throw new ErroreCompilazione(
            laRigaEQuellaGiusta
              ? `Nel listino ${nome} la riga ${riga} è quella giusta — la descrizione coincide — ` +
                  `ma l'EAN ${eanDelPiano} del piano sta nella colonna ${letteraDiColonna(altrove)} ` +
                  `e non in ${letteraDiColonna(rule.verify.eanColumn)}, che è la colonna dichiarata ` +
                  `per il controllo: da correggere è la configurazione di ${nome}, non il listino. ` +
                  `L'ordine ${nome} non viene creato.`
              : `Nel listino ${nome} la riga ${riga} porta l'EAN ${eanDelListino || "vuoto"} nella ` +
                  `colonna ${letteraDiColonna(rule.verify.eanColumn)} dichiarata per il controllo, ` +
                  `mentre l'EAN ${eanDelPiano} che il piano si aspetta sta nella colonna ` +
                  `${letteraDiColonna(altrove)} della stessa riga: o la colonna dichiarata è ` +
                  `sbagliata, o quella riga non è più quella dell'ordine. L'ordine ${nome} non ` +
                  `viene creato.`,
          );
        }
        throw new ErroreCompilazione(
          `La riga ${riga} del listino ${nome} porta l'EAN ${eanDelListino || "vuoto"} e il piano ` +
            `si aspetta ${eanDelPiano}: non è più la riga su cui è stato costruito l'ordine. ` +
            `L'ordine ${nome} non viene creato.`,
        );
      }
      verificate += 1;
      continue;
    }

    const descrizioneDelPiano = String(order.supplier_description ?? "").trim();

    if (rule.verify.eanColumn && eanDelListino) {
      // ⚠ Prima si usciva di qui con un avviso senza nemmeno guardare la
      // descrizione, che il registro dichiara e che qui e' l'unica cosa che sa
      // dire se la riga e' quella (revisione avversariale del 14 agosto 2026).
      // Per gli espositori LARICE — che nel piano l'EAN non ce l'hanno — voleva
      // dire un avviso a settimana su una riga che la descrizione conferma.
      const descrizioneDelListino = rule.verify.descriptionColumn
        ? String(valoreDiCella(used, usedValues, riga, rule.verify.descriptionColumn) ?? "").trim()
        : "";
      if (descrizioneDelPiano && descrizioneDelListino &&
          normalizzaDescrizione(descrizioneDelListino) === normalizzaDescrizione(descrizioneDelPiano)) {
        verificate += 1;
        continue;
      }
      avvisi.push(
        descrizioneDelPiano && descrizioneDelListino
          ? `La riga ${riga} del listino ${nome} porta l'EAN ${eanDelListino}, che il piano non ` +
              `dichiara, ed è descritta «${descrizioneDelListino}» mentre il piano dice ` +
              `«${descrizioneDelPiano}»: la quantità è stata scritta lo stesso, controlla che sia ` +
              "la riga giusta."
          : `Il piano non dice quale prodotto sia la riga ${riga}, ma il listino ${nome} lì porta ` +
              `l'EAN ${eanDelListino}: la quantità è stata scritta lo stesso, ` +
              "controlla che sia la riga giusta.",
      );
      nonVerificabili += 1;
      continue;
    }

    if (rule.verify.descriptionColumn && descrizioneDelPiano) {
      const trovata = String(
        valoreDiCella(used, usedValues, riga, rule.verify.descriptionColumn) ?? "",
      ).trim();
      if (normalizzaDescrizione(trovata) === normalizzaDescrizione(descrizioneDelPiano)) {
        verificate += 1;
      } else {
        avvisi.push(
          `La riga ${riga} del listino ${nome} è descritta «${trovata || "niente"}» e il piano ` +
            `dice «${descrizioneDelPiano}»: la quantità è stata scritta lo stesso, ` +
            "controlla che sia la riga giusta.",
        );
        nonVerificabili += 1;
      }
      continue;
    }

    nonVerificabili += 1;
  }

  return { verificate, nonVerificabili, avvisi };
}

async function prepareCopy({ source, supplier, rule, orders, outputDir, stagingDir, indice }) {
  await verifySourceHash(source, rule, supplier);
  const nome = supplierName(supplier);
  let libro;
  try {
    libro = await apriLibro(source);
  } catch (errore) {
    if (!(errore instanceof ContenitoreNonLeggibile)) throw errore;
    throw new ErroreCompilazione(
      `Il listino ${nome} non si apre come un documento Excel (.xlsx): ${errore.message} ` +
        `L'ordine ${nome} non viene creato.`,
    );
  }
  try {
    const sheet = worksheetForRule(libro, rule, supplier);
    verifyOrderHeader(sheet, rule, supplier);
    const used = sheet.griglia();
    const usedValues = used.values ?? [];
    const lastRow = Math.max(rule.dataStartRow, (used.rowIndex ?? 0) + (used.rowCount ?? usedValues.length));
    const quantities = orderRowsForSupplier(orders, supplier);
    for (const row of quantities.keys()) {
      if (row < rule.dataStartRow || row > lastRow) {
        throw new ErroreCompilazione(`Riga ${nome} non valida: ${row}.`);
      }
    }

    // Prima di toccare qualunque cella: la riga di destinazione porta ancora il
    // prodotto del piano?  Sta qui, e non dopo la scrittura, perche' anche
    // l'azzeramento e' una modifica, e per il fornitore che sbaglia riga non deve
    // uscire niente.
    const verifica = verificaRigheDiDestinazione({ used, usedValues, rule, supplier, orders });

    // Sempre prima di toccare una cella: nella colonna d'ordine non ci devono
    // essere formule, in nessuna riga fra la prima dei dati e l'ultima.  Dal 6
    // settembre 2026, perche' l'azzeramento tocca solo le celle che portano una
    // quantita', e una formula senza risultato memorizzato una quantita' non
    // sembra: usciva dentro la copia e la ricalcolava Excel (R7).
    sheet.rifiutaFormuleNellaColonna(rule.orderColumn, rule.dataStartRow, lastRow);
    const azzerate = azzeraQuantitaPreesistenti(sheet, rule, used, usedValues, lastRow);
    for (const [row, quantity] of quantities.entries()) {
      sheet.scriviNumero(`${rule.orderColumn}${row}`, quantity);
    }

    const destination = copyDestination(source, supplier, outputDir);
    // ⚠ Nello spazio temporaneo il nome lo fa l'**identificativo**, non il nome
    // leggibile: `nomePerIlFile` schiaccia in `_` tutto cio' che non e' A-Z0-9,
    // quindi «Sapori & Co.» e «Sapori Co» danno lo stesso file. Con il basename
    // della destinazione, due fornitori che collidono si sovrascrivevano qui
    // dentro: il secondo `rename` moriva `ENOENT` e cadeva la compilazione di
    // tutti, lasciando nella cartella un documento col nome di un fornitore e
    // dentro l'ordine di un altro (revisione del 14 agosto 2026). Gli
    // identificativi sono le chiavi di `supplier_files`: unici per costruzione.
    // Il numero davanti chiude anche il caso in cui due identificativi diversi
    // si riducano allo stesso nome ripulito.
    const nomeTemporaneo = `${indice}_${supplier.replace(/[^a-z0-9_-]+/gi, "_")}`;
    const staged = path.join(stagingDir, `${nomeTemporaneo}${path.extname(destination)}`);
    await libro.salva(staged);
    return {
      supplier,
      source,
      staged,
      destination,
      written_rows: quantities.size,
      total_colli: [...quantities.values()].reduce((total, quantity) => total + quantity, 0),
      cleared_rows: azzerate,
      verified_rows: verifica.verificate,
      unverifiable_rows: verifica.nonVerificabili,
      warnings: verifica.avvisi,
    };
  } catch (errore) {
    // Il foglio ha detto di no, in italiano: una formula nella colonna
    // d'ordine, una griglia che non e' quella di un listino.  E' una frase per
    // l'utente, e va marcata come tale.
    if (!(errore instanceof FoglioNonScrivibile)) throw errore;
    throw new ErroreCompilazione(`${errore.message} L'ordine ${nome} non viene creato.`);
  }
}

async function removeCopy(destination) {
  await fs.rm(destination, { force: true });
}

/**
 * Le copie vecchie di **questo** listino, comunque si chiamassero.
 *
 * ⚠ `copyDestination` costruisce il nome con il `display_name` di **oggi**.
 * Bastava che il nome del fornitore cambiasse — un fornitore imparato che
 * l'utente ribattezza, un `display_name` corretto nel registro — perche' la
 * copia della volta prima si chiamasse in un altro modo: la pulizia non la
 * toccava, restava nella cartella d'uscita e usciva insieme a quella nuova,
 * con dentro le quantita' di allora.  Due ordini per lo stesso fornitore, e
 * niente che lo dica.
 *
 * Il legame che regge un cambio di nome e' il **listino di partenza**: il
 * nome del file d'ordine finisce sempre con `_<nome del listino>.xlsx`.
 * `protette` sono le copie appena prodotte in questa esecuzione, che non si
 * toccano nemmeno se due fornitori dichiarassero lo stesso listino.
 *
 * ⚠ Il nome del listino puo' essere la **coda** di quello di un altro
 * fornitore: con `listino` e `mio_listino` fra i documenti configurati,
 * `ORDINE_B_mio_listino.xlsx` finisce per `_listino` e la pulizia del primo si
 * portava via l'ordine del secondo (revisione avversariale del 14 agosto
 * 2026). Fra tutti i listini configurati vince la coda **piu' lunga**: e' la
 * stessa regola con cui si legge un nome di file ovunque, e senza di lei
 * questa funzione cancella per omonimia.
 */
async function removeOldCopies(source, outputDir, protette = new Set(), basiConfigurate = []) {
  const base = path.parse(path.resolve(source)).name;
  const nomi = await fs.readdir(outputDir).catch(() => []);
  for (const nome of nomi) {
    const parti = path.parse(nome);
    if (parti.ext.toLowerCase() !== ".xlsx") continue;
    if (!parti.name.startsWith("ORDINE_") || !parti.name.endsWith(`_${base}`)) continue;
    const piuSpecifica = [...basiConfigurate, base]
      .filter((candidata) => parti.name.endsWith(`_${candidata}`))
      .reduce((piuLunga, candidata) => (candidata.length > piuLunga.length ? candidata : piuLunga), "");
    if (piuSpecifica !== base) continue;
    const percorso = path.join(outputDir, nome);
    if (protette.has(percorso)) continue;
    await removeCopy(percorso);
  }
}

function selectedSuppliers(orders) {
  const selected = new Set();
  for (const order of orders) {
    const supplier = normalizeSupplierId(order?.supplier);
    if (!supplier) throw new ErroreCompilazione("Il piano contiene una riga senza fornitore.");
    selected.add(supplier);
  }
  return selected;
}

async function compila() {
  const argv = argsOf(process.argv);
  let plan;
  try {
    plan = JSON.parse(await fs.readFile(argv.plan, "utf8"));
  } catch (errore) {
    throw new ErroreCompilazione("Il piano ordini non si riesce a leggere.", { cause: errore });
  }
  if (!Array.isArray(plan.orders)) throw new ErroreCompilazione("Il piano ordini non contiene una lista di righe.");
  const config = await loadWriterConfig(argv);
  const configDirectory = argv.config ? path.dirname(path.resolve(argv.config)) : process.cwd();
  const supplierFiles = normalizedObjectMap(config.supplier_files, "supplier_files");
  const supplierRules = normalizedObjectMap(config.supplier_write_rules || {}, "supplier_write_rules");
  // Prima di qualunque frase: i nomi leggibili si registrano subito, cosi' anche
  // gli errori sulla configurazione stessa chiamano il fornitore con il suo nome.
  registraNomiDichiarati(supplierRules);
  const outputDir = path.resolve(argv["output-dir"]);
  // I nomi di tutti i listini configurati: servono alla pulizia delle copie
  // vecchie per non cancellare per omonimia (`listino` e' la coda di
  // `mio_listino`).
  const basiConfigurate = Object.values(supplierFiles)
    .filter((originale) => typeof originale === "string" && originale.trim())
    .map((originale) => path.parse(
      path.resolve(path.isAbsolute(originale) ? originale : path.join(configDirectory, originale)),
    ).name);
  const orders = plan.orders.filter((entry) => entry && typeof entry === "object");
  if (orders.length !== plan.orders.length) throw new ErroreCompilazione("Il piano ordini contiene una riga non valida.");
  const selected = selectedSuppliers(orders);

  const configured = new Map();
  for (const supplier of selected) {
    if (scrittoAltrove(supplierRules, supplier)) continue;
    const original = supplierFiles[supplier];
    if (typeof original !== "string" || !original.trim()) {
      throw new ErroreCompilazione(`Il fornitore ${supplierName(supplier)} è selezionato ma non ha un listino configurato per la compilazione.`);
    }
    const source = path.resolve(path.isAbsolute(original) ? original : path.join(configDirectory, original));
    const sourceStats = await fs.stat(source).catch(() => null);
    if (!sourceStats?.isFile() || path.extname(source).toLowerCase() !== ".xlsx") {
      throw new ErroreCompilazione(`Il listino configurato per ${supplierName(supplier)} non è disponibile o non è un file XLSX.`);
    }
    const rule = normalizeRule(configuredRule(supplierRules, supplier), supplier);
    configured.set(supplier, { source, rule });
  }

  await fs.mkdir(outputDir, { recursive: true });
  const stagingDir = await fs.mkdtemp(path.join(outputDir, ".ordine-temporaneo-"));
  try {
    const prepared = [];
    for (const [supplier, { source, rule }] of configured.entries()) {
      const supplierOrders = orders.filter((entry) => normalizeSupplierId(entry.supplier) === supplier);
      prepared.push(await prepareCopy({
        source, supplier, rule, orders: supplierOrders, outputDir, stagingDir,
        indice: prepared.length,
      }));
    }
    // Due fornitori diversi che finirebbero sullo stesso documento: il writer
    // non sa quale dei due ordini consegnare, e sceglierne uno vorrebbe dire
    // mandare a un fornitore la merce di un altro.
    const perDestinazione = new Map();
    for (const preparedCopy of prepared) {
      const gia = perDestinazione.get(preparedCopy.destination);
      if (gia) {
        throw new ErroreCompilazione(
          `${supplierName(gia)} e ${supplierName(preparedCopy.supplier)} produrrebbero lo stesso ` +
            `documento d'ordine (${path.basename(preparedCopy.destination)}): cambia il nome ` +
            "dichiarato per uno dei due nel registro dei fornitori. Non viene creata nessuna copia.",
        );
      }
      perDestinazione.set(preparedCopy.destination, preparedCopy.supplier);
    }

    // Chi si scrive in posizione non passa di qui: a Noce si rimanda il
    // **loro** `.xls`, compilato dal servizio locale (`app/xls_writer.py`).  Qui
    // le sue righe si contano soltanto, per dirlo nel riepilogo — e chi sia lo
    // dice la configurazione, non un nome scritto in questo file.
    const noceOrders = orders.filter(
      (entry) => scrittoAltrove(supplierRules, normalizeSupplierId(entry.supplier)),
    );

    // Tutte le verifiche, importazioni e esportazioni sono già riuscite nello
    // spazio temporaneo. Solo ora vengono sostituite le copie visibili — e con
    // loro se ne vanno anche le copie vecchie dello stesso listino che oggi si
    // chiamerebbero in un altro modo.
    for (const preparedCopy of prepared) {
      await removeOldCopies(preparedCopy.source, outputDir, new Set(), basiConfigurate);
    }
    for (const preparedCopy of prepared) await fs.rename(preparedCopy.staged, preparedCopy.destination);
    const appenaProdotte = new Set(prepared.map((preparedCopy) => preparedCopy.destination));

    const results = [];
    for (const [supplier, original] of Object.entries(supplierFiles)) {
      if (scrittoAltrove(supplierRules, supplier) || typeof original !== "string" || !original.trim()) continue;
      const active = prepared.find((entry) => entry.supplier === supplier);
      if (active) {
        results.push({
          supplier,
          // Il nome leggibile viaggia accanto all'identificativo tecnico: chi
          // legge il riepilogo non deve rifarsi la tabella dei nomi per conto
          // suo — e' esattamente il modo in cui `NUOVO_FORNITORE` e' finito
          // sotto gli occhi dell'utente.
          supplier_name: supplierName(supplier),
          destination: active.destination,
          written_rows: active.written_rows,
          total_colli: active.total_colli,
          cleared_rows: active.cleared_rows,
          verified_rows: active.verified_rows,
          unverifiable_rows: active.unverifiable_rows,
          warnings: active.warnings,
        });
        continue;
      }
      const originalPath = path.resolve(path.isAbsolute(original) ? original : path.join(configDirectory, original));
      // Configurato ma non ordinato: se una compilazione precedente aveva
      // lasciato qui la sua copia, quella copia se ne va — anche se allora il
      // fornitore si chiamava in un altro modo.  Le copie appena prodotte in
      // questa esecuzione sono protette: due fornitori che dichiarassero lo
      // stesso listino non devono cancellarsi l'ordine a vicenda.
      await removeOldCopies(originalPath, outputDir, appenaProdotte, basiConfigurate);
      // Le stesse chiavi anche qui: un riepilogo dove i conteggi ci sono solo
      // per certi fornitori si legge male e si somma peggio.
      results.push({
        supplier,
        supplier_name: supplierName(supplier),
        destination: null,
        written_rows: 0,
        total_colli: 0,
        verified_rows: 0,
        unverifiable_rows: 0,
        warnings: [],
        skipped: true,
      });
    }

    const riepilogo = {
      supplier_copies: results,
      noce_lines: noceOrders.length,
      noce_note: noceOrders.length
        ? "Le righe Noce le compila il servizio locale dentro una copia del loro .xls."
        : null,
    };
    // Il riepilogo esce due volte, e non e' una svista: quello marcato e' per il
    // programma che ci chiama, quello indentato per gli occhi sulla console.
    //
    // ⚠ La marca resta anche adesso che su `stdout` scriviamo solo noi — fino
    // al 5 settembre 2026 la libreria dei fogli di calcolo ci aggiungeva una
    // riga sua a ogni salvataggio, e chi leggeva tutto lo `stdout` come JSON si
    // fermava li'.  Fa per i conteggi quello che `ERRORE_COMPILAZIONE:` fa per
    // le frasi: una riga sola, riconoscibile, che chi ci chiama sa cercare.
    console.log(MARCA_RIEPILOGO + JSON.stringify(riepilogo));
    // ⚠ E il blocco indentato resta l'**ultima** cosa che esce: chi lo legge oggi
    // (`tests/test_web_app.py`) taglia `stdout` dal primo `{\n  "supplier_copies"`
    // fino in fondo, e una riga in piu' dopo di lui gli darebbe «Extra data».
    console.log(JSON.stringify(riepilogo, null, 2));
  } finally {
    await fs.rm(stagingDir, { recursive: true, force: true });
  }
}

try {
  await compila();
} catch (errore) {
  // In pagina arriva UNA riga marcata, in italiano, senza traccia di Node ne'
  // percorsi assoluti — e soltanto se la frase l'ha scritta il writer.  Tutto
  // il resto (la traccia, il `cause`) esce NON marcato: `app/server.py` lo
  // tiene sulla console del programma, che e' il posto dove serve.
  if (errore instanceof ErroreCompilazione) {
    if (errore.cause !== undefined) {
      console.error(String(errore.cause?.stack ?? errore.cause));
    }
    const frase = String(errore.message ?? "").split("\n")[0].trim();
    console.error(MARCA_ERRORE + (frase || FRASE_GUASTO_IMPREVISTO));
  } else {
    console.error(String(errore?.stack ?? errore ?? ""));
    console.error(MARCA_ERRORE + FRASE_GUASTO_IMPREVISTO);
  }
  process.exitCode = 1;
}
