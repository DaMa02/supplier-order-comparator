#!/usr/bin/env node
import crypto from "node:crypto";
import { createReadStream } from "node:fs";
import fs from "node:fs/promises";
import path from "node:path";

import { ContenitoreNonLeggibile, FoglioNonScrivibile, apriLibro } from "./lib/xlsx_in_posizione.mjs";

// Every error surfaces on a single line with this marker, for the caller
// (`app/server.py`) to grep instead of forwarding Node's raw stack trace
// with absolute paths to the user.
const MARCA_ERRORE = "ERRORE_COMPILAZIONE: ";

// The end-of-run summary is marked the same way, on a single line, as
// compact JSON: the caller greps for the marked line rather than parsing
// the whole `stdout` (see the bottom of the file).
const MARCA_RIEPILOGO = "RIEPILOGO_COMPILAZIONE: ";

/**
 * A failure the writer can explain, with a message already written for
 * the user.
 *
 * The `ERRORE_COMPILAZIONE:` marker must only go on messages of this
 * class. Marking whatever comes out of the final `catch` would surface
 * other errors — raw ENOENT paths, .NET resource keys from a dependency —
 * dressed up as if they were a real explanation. A message the writer
 * didn't compose itself is a technical detail for the console, not
 * something to show the user.
 */
class ErroreCompilazione extends Error {}

const FRASE_GUASTO_IMPREVISTO =
  "la creazione delle copie dei listini si è fermata per un guasto imprevisto; " +
  "i dettagli tecnici sono sulla console del programma. " +
  "Il piano ordini è completo e si può scaricare.";

// Copies are produced by `lib/xlsx_in_posizione.mjs`, which touches only
// the sheet XML inside the ZIP and copies every other part byte for byte,
// instead of a full-reserialization library that would risk dropping
// cells, section headers, or the shape Excel expects.

const SUPPLIER_NAMES = {
  betulla: "BETULLA",
  cipresso: "CIPRESSO",
  larice: "LARICE",
  noce: "NOCE",
};

/**
 * Human-readable supplier names declared by the config (`display_name`).
 *
 * A supplier's display name is data from the adapter registry, not the
 * code: it arrives with the write config, and the hardcoded `SUPPLIER_NAMES`
 * table below is only a fallback for configs that don't declare one — using
 * a raw supplier id there instead would leak an internal identifier into
 * user-facing messages and into the order filename itself.
 *
 * This map is filled once, right after the config is read, because
 * `supplierName` is called from many places scattered through the file,
 * some before that supplier's rule has even been validated.
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

/** Strips accents so accented and unaccented spellings of a word compare equal. */
function senzaAccenti(testo) {
  return String(testo ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

/**
 * The supplier's name reduced to characters a filename can carry.
 *
 * User-facing messages use the name as the registry declared it, but that
 * same name also goes into `ORDINE_<name>_<pricelist>.xlsx`, and Windows
 * rejects colons, slashes, asterisks and question marks in filenames — the
 * file would simply fail to write and the supplier would get no order.
 * Only the message-facing name is affected; the sanitized form is used for
 * the filename only.
 */
function nomePerIlFile(supplier) {
  const ripulito = senzaAccenti(supplierName(supplier))
    .toUpperCase()
    .replace(/[^A-Z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  // A name made only of punctuation would leave nothing behind; a
  // placeholder word beats a file literally named `ORDINE__listino.xlsx`.
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
 * The column to verify, declared as a letter ("A", "AB") or a 1-based
 * number — both are common in spreadsheet configs, and the registry may
 * use either. Always returns the number, since that's how the already
 * loaded grid is indexed.
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
 * Price-list columns to compare against the plan before writing.
 *
 * Both optional and independent: `ean_column` and `description_column` can
 * both be set, only one, or neither. Without `verify`, or with an empty
 * one, the write behaves as it always did, so an older config keeps
 * working without this key.
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

/** `A` becomes 1, `D` becomes 4: column number as Excel counts it. */
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
    // No hardcoded per-supplier fallback on purpose: rules come from the
    // adapter registry via the config, and a writer that hardcodes columns
    // itself can silently write an order into a column that doesn't match
    // the one the registry declares.
    throw new ErroreCompilazione(
      "Serve la configurazione di scrittura (--config): le regole vengono dal registro, non dal codice.",
    );
  }
  const configPath = path.resolve(argv.config);
  let config;
  try {
    config = JSON.parse(await fs.readFile(configPath, "utf8"));
  } catch (error) {
    // The underlying detail (ENOENT, a JSON syntax error) carries absolute
    // paths; it goes to the console as `cause`, not into the user message.
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
 * Whether this supplier's price list is written by the local `.xls`
 * service instead of this writer.
 *
 * Decided by the write procedure the config declares (read by
 * `app/server.py` as `procedura_di_scrittura`, and carried here from the
 * adapter registry as `compilazione`), never by a hardcoded supplier
 * name. A supplier id check here would misroute the moment a supplier
 * changes format, or a new supplier with that same name needs the
 * opposite path.
 *
 * A supplier that declares nothing falls through here: the normal path,
 * shared by every `.xlsx` price list.
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
  // There's deliberately no per-supplier hardcoded expectation here (a
  // fixed column letter or header text for a named supplier): a rule
  // rejected in the code contradicts a mapping the user already confirmed
  // and the registry already declares, and a single such mismatch would
  // block every other supplier's fill too. What's verified is exactly
  // what the config declares: if it names header text, that text must be
  // there; if it says the user confirmed the header cell is blank, that
  // cell must still be blank; if it declares no header at all (some price
  // lists have no header row), there's nothing to check, and the write is
  // still defended by sheet, column, start row, source hash, and the EAN
  // and description checks on the destination row.
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
  // A confirmed "that cell is blank" is a check like any other, redone
  // here against the open document: between confirmation and this run the
  // supplier's file could have gained a header where there wasn't one,
  // and writing beneath an unread header is exactly the failure these
  // guards exist to prevent.
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

/** Is what's stored in a cell a valid order quantity? */
function eUnaQuantita(valore) {
  if (valore === null || valore === undefined || typeof valore === "boolean") return false;
  if (typeof valore === "number") return Number.isFinite(valore);
  const testo = String(valore).trim();
  if (!testo) return false;
  return Number.isFinite(Number(testo.replace(",", ".")));
}

/** `[3,4,5,9]` -> `[[3,5],[9,9]]`: contiguous runs, so they can be cleared in one call each. */
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
 * Zeroes out quantities already written in the order column, and only those.
 *
 * Clearing the whole column outright would also erase non-product rows —
 * some price lists use the same column for section headers, not just
 * quantities. Zeroing stale quantities is still necessary, since otherwise
 * a previous run's quantities would ship again as phantom order lines, but
 * a row that isn't a product has no quantity to zero: it holds text, and
 * text is left alone.
 *
 * Same rule as elsewhere in this program: a quantity is a number. It's the
 * same rule `app/xls_writer.py` applies to its `.xls` target, where an
 * order-column cell that isn't a fixed-length number stops the write.
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

/** EAN normalized for comparison: `8000000000011.0` and `8000000000011` are the same code. */
function normalizzaEan(valore) {
  if (valore === null || valore === undefined || typeof valore === "boolean") return "";
  const testo = String(valore).trim();
  // An EAN is digits, but spreadsheets often store it as a number: the
  // plan can carry a trailing ".0" while the price list reads it as an
  // integer (or the reverse), and two spellings of the same code must not
  // look like two different products.
  return /^\d+\.0*$/.test(testo) ? testo.replace(/\.0*$/, "") : testo;
}

/**
 * Description normalized for comparison: uppercased, accents stripped,
 * any run of non-alphanumeric characters collapsed to a single space.
 *
 * The comparison is deliberately loose: the plan's description may have
 * been cleaned upstream (an expiry date removed, double spaces collapsed),
 * and an exact-text comparison would block a correct order over a comma.
 */
function normalizzaDescrizione(valore) {
  return senzaAccenti(valore).toUpperCase().replace(/[^A-Z0-9]+/g, " ").trim();
}

/**
 * What's stored at `(row, column)` in the source price list.
 *
 * Reads from the grid already loaded via `griglia()`, same as
 * `azzeraQuantitaPreesistenti`: a per-cell read on a price list with
 * thousands of rows adds up.
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

/** Column number written the way a spreadsheet writes it: 1 -> "A", 28 -> "AB". */
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
 * Which column of that row actually holds the code the plan is looking for.
 *
 * A misconfigured `ean_column` produces the same symptom as a price list
 * that genuinely changed — "that row no longer has that product" — and
 * points debugging at the wrong place: the price list is fine, it's the
 * config that names the wrong column. If the plan's code sits right next
 * to it, in another column of the same row, that's worth surfacing in the
 * error rather than reporting a generic mismatch.
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
 * Whether the destination row still holds the product the plan expects.
 *
 * Writing a quantity by `column+row` alone trusts the row number blindly:
 * the price list's sha256 guards against the whole file changing, not
 * against a wrong `supplier_source_row`, and a shifted or reordered price
 * list can silently move a quantity onto the wrong product's row. This is
 * the EAN check that guards against that, matching the same check
 * `app/xls_writer.py` runs for its `.xls` target.
 *
 * Where this differs from that check, and why:
 *
 * - A plan with no EAN but a price list row that has one is a warning, not
 *   an error: some parent rows (e.g. a display's parent line) carry an EAN
 *   that the plan doesn't, and failing closed there would block legitimate
 *   orders.
 * - A mismatched description is also a warning: the plan's description
 *   may have been cleaned upstream, and treating that as fatal would
 *   discard good orders.
 *
 * A mismatched EAN stays an error: that row no longer holds the plan's
 * product, so nothing is created for that supplier.
 *
 * Counted per order row, not per price-list row: `verificate` plus
 * `nonVerificabili` add up to the rows the plan carries for that supplier.
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
      // An empty price-list cell falls into this branch on purpose: if the
      // plan has an EAN and the row no longer carries it, that row is no
      // longer that product.
      if (eanDelListino !== eanDelPiano) {
        const altrove = colonnaDoveStaDavvero(used, usedValues, riga, eanDelPiano);
        // Finding the plan's EAN in another column doesn't by itself mean
        // the declared column is wrong: a supplier that repeats the code
        // in the item-name column will also do so on a row that no longer
        // holds that product. Trusting the wrong signal would lead someone
        // to repoint `ean_column` and permanently disable this defense, so
        // the description is checked too, since it's the only other signal
        // that can tell the two cases apart; when it's unavailable, both
        // possibilities are stated.
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
          // The write still stops here — an unresolved configuration
          // mismatch is not written through — but the message points to
          // where to look, stated as certain only when the description
          // confirms it's the right row.
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
      // The description is checked here rather than warning immediately:
      // for rows whose plan carries no EAN (e.g. a display's parent row),
      // this is the only signal that can still confirm the row is right.
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

    // Before touching any cell: does the destination row still hold the
    // plan's product? Runs here, before any write, because zeroing is
    // already a modification, and a supplier whose row mapping is wrong
    // must get nothing written at all.
    const verifica = verificaRigheDiDestinazione({ used, usedValues, rule, supplier, orders });

    // Also before any write: the order column must have no formulas in
    // any row between the first data row and the last. This has to be a
    // separate pass, since zeroing only touches cells that already hold a
    // quantity, and a formula with no cached result doesn't look like one —
    // it would otherwise survive into the copy and get recalculated by Excel.
    sheet.rifiutaFormuleNellaColonna(rule.orderColumn, rule.dataStartRow, lastRow);
    const azzerate = azzeraQuantitaPreesistenti(sheet, rule, used, usedValues, lastRow);
    for (const [row, quantity] of quantities.entries()) {
      sheet.scriviNumero(`${rule.orderColumn}${row}`, quantity);
    }

    const destination = copyDestination(source, supplier, outputDir);
    // The staged file is named by the supplier id, not the display name:
    // `nomePerIlFile` collapses anything outside A-Z0-9 to `_`, so two
    // different display names can sanitize to the same string. Naming the
    // staged file after the destination's basename would let two colliding
    // suppliers overwrite each other here, and a later failed `rename`
    // would fail the whole batch while leaving a mismatched file behind.
    // Supplier ids are the keys of `supplier_files`, unique by
    // construction; the leading index also covers the case where two
    // different ids still sanitize to the same string.
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
    // The sheet refused (a formula in the order column, a grid shape that
    // isn't a price list): the message is already user-facing and gets
    // marked as such.
    if (!(errore instanceof FoglioNonScrivibile)) throw errore;
    throw new ErroreCompilazione(`${errore.message} L'ordine ${nome} non viene creato.`);
  }
}

async function removeCopy(destination) {
  await fs.rm(destination, { force: true });
}

/**
 * Old copies for this price list, whatever name they were written under.
 *
 * `copyDestination` builds the filename from the current `display_name`.
 * If a supplier's display name changes between runs — the user renames a
 * learned supplier, or a `display_name` is corrected in the registry — a
 * cleanup keyed only on today's name would miss the previous run's copy,
 * leaving it in the output folder alongside the new one with its stale
 * quantities. Two orders on file for the same supplier, with nothing
 * flagging it.
 *
 * The stable link across a rename is the source price list: an order
 * filename always ends with `_<price list name>.xlsx`. `protette` are the
 * copies just produced in this run, left untouched even if two suppliers
 * happen to declare the same price list.
 *
 * A price list's name can be a suffix of another supplier's price-list
 * name (e.g. `listino` and `mio_listino` both configured): a naive suffix
 * match on the shorter name would also match, and delete, the other
 * supplier's order. Among all configured price-list names, the longest
 * matching suffix wins — the same rule used anywhere a filename is parsed
 * this way — otherwise this function deletes files by accidental name
 * overlap.
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
  // Display names are registered before any message is built, so even an
  // error about the config itself refers to the supplier by name.
  registraNomiDichiarati(supplierRules);
  const outputDir = path.resolve(argv["output-dir"]);
  // Names of every configured price list, needed by the old-copy cleanup
  // to avoid deleting by accidental suffix overlap.
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
    // Two different suppliers that would land on the same document: the
    // writer has no way to know which order to deliver, and picking one
    // would mean sending another supplier's goods to the wrong recipient.
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

    // A supplier written by in-place `.xls` patching doesn't go through
    // this path; their own `.xls` is filled by the local service
    // (`app/xls_writer.py`) instead. Here, only its row count is tallied
    // for the summary — and which supplier that is comes from the
    // config, never a hardcoded name in this file.
    const noceOrders = orders.filter(
      (entry) => scrittoAltrove(supplierRules, normalizeSupplierId(entry.supplier)),
    );

    // All checks and writes have already succeeded in the staging
    // directory. Only now are the visible copies replaced — and with
    // them, old copies of the same price list that would be named
    // differently today are removed too.
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
          // The display name travels alongside the technical id, so a
          // reader of the summary doesn't have to reconstruct the name
          // table itself.
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
      // Configured but not ordered this time: a copy left behind by a
      // previous run is removed, even if the supplier's name was
      // different back then. Copies just produced in this run are
      // protected, so two suppliers sharing the same price list can't
      // delete each other's order.
      await removeOldCopies(originalPath, outputDir, appenaProdotte, basiConfigurate);
      // Same fields as an active copy: a summary where the counts only
      // exist for some suppliers is harder to read and to sum.
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
    // The summary is printed twice on purpose: the marked line for the
    // calling program to grep, the indented block for a human reading the
    // console. The marker lets the caller find the summary line reliably
    // even if this process's `stdout` ever carries other output, the same
    // way `ERRORE_COMPILAZIONE:` marks error lines.
    console.log(MARCA_RIEPILOGO + JSON.stringify(riepilogo));
    // The indented block stays the LAST thing printed: `tests/test_web_app.py`
    // slices `stdout` from the first `{\n  "supplier_copies"` to the end, and
    // any line printed after it would break that JSON parse with "Extra data".
    console.log(JSON.stringify(riepilogo, null, 2));
  } finally {
    await fs.rm(stagingDir, { recursive: true, force: true });
  }
}

try {
  await compila();
} catch (errore) {
  // Only ONE marked line reaches the user-facing page, and only if the
  // writer itself composed the message — no Node stack trace, no absolute
  // paths. Everything else (the stack, the `cause`) is printed unmarked;
  // `app/server.py` keeps that on the program's console, where it belongs.
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
