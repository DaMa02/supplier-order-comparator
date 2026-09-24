// Writes order quantities into a copy of a `.xlsx` file, touching ONLY the
// order sheet, and on that sheet only the cells of the order column.
//
// WHY THIS MODULE EXISTS
// A full-reserialization library that imports the whole document, rebuilds
// it in memory and rewrites it from scratch will silently drop or alter
// content it doesn't fully understand — dropped section headers, corrupted
// values, copies Excel then refuses to open. The cell-by-cell guard in
// `app/copia_fedele.py` proves the values, not the shape of the file, but
// it's a safety net, not a substitute for not causing the damage.
//
// Nothing is reconstructed here. A `.xlsx` is a ZIP containing XML: the ZIP
// is opened with plain `zlib`, only the text of the cells being written in
// the order sheet is changed, and the ZIP is closed again by copying every
// other part byte-for-byte as it was — styles, formulas, drawings, merged
// cells, defined names, all of it. The document stays the supplier's own,
// with only the order column filled in: the same principle as the in-place
// `.xls` patch in `app/xls_writer.py`.
//
// No dependency beyond `node:zlib` and `node:fs`.
//
// What this deliberately does NOT do:
// - write text: a quantity is a number, and a number needs no
//   `sharedStrings.xml` entry;
// - touch a cell that carries a formula: it stops and says so, because
//   overwriting a supplier's formula is exactly the wrong move;
// - recalculate totals: if the sheet has formulas, it asks Excel to
//   recalculate on open (`fullCalcOnLoad` in `workbook.xml`).

import { readFile, writeFile } from "node:fs/promises";
import { deflateRawSync, inflateRawSync } from "node:zlib";

const FIRMA_FINE_DIRECTORY = 0x06054b50;
const FIRMA_VOCE_DIRECTORY = 0x02014b50;
const FIRMA_INTESTAZIONE_LOCALE = 0x04034b50;
// A ZIP's trailing comment length is a 2-byte field: at most 65,535
// characters, plus the 22-byte end-of-central-directory record itself.
const MASSIMO_COMMENTO = 65535 + 22;
const METODO_DEFLATE = 8;
const METODO_NESSUNO = 0;
// Flag bit 3 means "sizes are in a descriptor after the data"; headers are
// rewritten here with the sizes inline, so this bit must be cleared.
const FLAG_DESCRITTORE = 0x0008;
// Above this many cells the grid isn't built: a sheet declaring a cell at
// XFD1048576 isn't a price list, it's a corrupted file.
const MASSIME_CELLE_IN_GRIGLIA = 50_000_000;

export class ContenitoreNonLeggibile extends Error {}
export class FoglioNonScrivibile extends Error {}

// ---------------------------------------------------------------------------
// ZIP: reading
// ---------------------------------------------------------------------------

/** Offset where the ZIP's end-of-central-directory record starts. */
function fineDirectory(dati) {
  const minimo = Math.max(0, dati.length - MASSIMO_COMMENTO);
  for (let posizione = dati.length - 22; posizione >= minimo; posizione -= 1) {
    if (dati.readUInt32LE(posizione) === FIRMA_FINE_DIRECTORY) return posizione;
  }
  throw new ContenitoreNonLeggibile("Il documento non è un contenitore ZIP valido.");
}

/**
 * Central directory entries, in file order.
 *
 * Keeps everything needed to rewrite the headers; a read-only caller only
 * uses about half these fields.
 */
export function elencoVoci(dati) {
  const fine = fineDirectory(dati);
  const quante = dati.readUInt16LE(fine + 10);
  const dimensione = dati.readUInt32LE(fine + 12);
  const inizio = dati.readUInt32LE(fine + 16);
  if (quante === 0xffff || dimensione === 0xffffffff || inizio === 0xffffffff) {
    // A price-list `.xlsx` never reaches 4 GB or 65,535 parts; if it does,
    // better to say so than to read garbage offsets.
    throw new ContenitoreNonLeggibile("Il documento usa il formato ZIP64: non è un listino.");
  }
  const voci = [];
  let posizione = inizio;
  for (let indice = 0; indice < quante; indice += 1) {
    if (posizione + 46 > dati.length || dati.readUInt32LE(posizione) !== FIRMA_VOCE_DIRECTORY) {
      throw new ContenitoreNonLeggibile("L'elenco delle parti del documento è rovinato.");
    }
    const lunghezzaNome = dati.readUInt16LE(posizione + 28);
    const lunghezzaExtra = dati.readUInt16LE(posizione + 30);
    const lunghezzaCommento = dati.readUInt16LE(posizione + 32);
    voci.push({
      nome: dati.toString("utf8", posizione + 46, posizione + 46 + lunghezzaNome),
      versioneCreatore: dati.readUInt16LE(posizione + 4),
      versioneNecessaria: dati.readUInt16LE(posizione + 6),
      flag: dati.readUInt16LE(posizione + 8),
      metodo: dati.readUInt16LE(posizione + 10),
      ora: dati.readUInt16LE(posizione + 12),
      data: dati.readUInt16LE(posizione + 14),
      crc: dati.readUInt32LE(posizione + 16),
      byteCompressi: dati.readUInt32LE(posizione + 20),
      byteVeri: dati.readUInt32LE(posizione + 24),
      attributiInterni: dati.readUInt16LE(posizione + 36),
      attributiEsterni: dati.readUInt32LE(posizione + 38),
      offsetLocale: dati.readUInt32LE(posizione + 42),
    });
    posizione += 46 + lunghezzaNome + lunghezzaExtra + lunghezzaCommento;
  }
  return voci;
}

/** The compressed bytes of an entry, exactly as they sit in the file. */
function byteDellaVoce(dati, voce) {
  const testa = voce.offsetLocale;
  if (testa + 30 > dati.length || dati.readUInt32LE(testa) !== FIRMA_INTESTAZIONE_LOCALE) {
    throw new ContenitoreNonLeggibile(`La parte «${voce.nome}» del documento non si trova dove dichiarato.`);
  }
  const lunghezzaNome = dati.readUInt16LE(testa + 26);
  const lunghezzaExtra = dati.readUInt16LE(testa + 28);
  const inizio = testa + 30 + lunghezzaNome + lunghezzaExtra;
  if (inizio + voce.byteCompressi > dati.length) {
    throw new ContenitoreNonLeggibile(`La parte «${voce.nome}» del documento è tagliata.`);
  }
  return dati.subarray(inizio, inizio + voce.byteCompressi);
}

/** The content of a part, already decompressed, as UTF-8 text. */
export function leggiParte(dati, voce) {
  const crudo = byteDellaVoce(dati, voce);
  if (voce.metodo === METODO_NESSUNO) return crudo.toString("utf8");
  if (voce.metodo === METODO_DEFLATE) return inflateRawSync(crudo).toString("utf8");
  throw new ContenitoreNonLeggibile(
    `La parte «${voce.nome}» del documento è compressa in un modo che non si sa leggere.`,
  );
}

// ---------------------------------------------------------------------------
// ZIP: writing
// ---------------------------------------------------------------------------

const TABELLA_CRC = (() => {
  const tabella = new Uint32Array(256);
  for (let n = 0; n < 256; n += 1) {
    let c = n;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    tabella[n] = c >>> 0;
  }
  return tabella;
})();

/** ZIP CRC-32, computed by hand: `zlib.crc32` only exists from Node 22 on. */
export function crc32(dati) {
  let crc = 0xffffffff;
  for (let i = 0; i < dati.length; i += 1) crc = TABELLA_CRC[(crc ^ dati[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/**
 * The rewritten ZIP: every part copied compressed exactly as it was,
 * except the replaced ones. All headers are rewritten fresh, with no extra
 * fields and no data descriptors — the simplest form every reader accepts,
 * while the data bytes of untouched parts stay the supplier's own.
 */
function costruisciZip(dati, voci, sostituite) {
  const locali = [];
  const centrali = [];
  let offset = 0;
  for (const voce of voci) {
    const nome = Buffer.from(voce.nome, "utf8");
    const nuova = sostituite.get(voce.nome);
    let corpo;
    let metodo = voce.metodo;
    let crc = voce.crc;
    let byteVeri = voce.byteVeri;
    if (nuova !== undefined) {
      const testo = Buffer.from(nuova, "utf8");
      corpo = deflateRawSync(testo);
      metodo = METODO_DEFLATE;
      crc = crc32(testo);
      byteVeri = testo.length;
    } else {
      corpo = byteDellaVoce(dati, voce);
    }
    const flag = voce.flag & ~FLAG_DESCRITTORE;
    const versioneNecessaria = Math.max(voce.versioneNecessaria, metodo === METODO_DEFLATE ? 20 : 10);

    const locale = Buffer.alloc(30);
    locale.writeUInt32LE(FIRMA_INTESTAZIONE_LOCALE, 0);
    locale.writeUInt16LE(versioneNecessaria, 4);
    locale.writeUInt16LE(flag, 6);
    locale.writeUInt16LE(metodo, 8);
    locale.writeUInt16LE(voce.ora, 10);
    locale.writeUInt16LE(voce.data, 12);
    locale.writeUInt32LE(crc, 14);
    locale.writeUInt32LE(corpo.length, 18);
    locale.writeUInt32LE(byteVeri, 22);
    locale.writeUInt16LE(nome.length, 26);
    locale.writeUInt16LE(0, 28);
    locali.push(locale, nome, corpo);

    const centrale = Buffer.alloc(46);
    centrale.writeUInt32LE(FIRMA_VOCE_DIRECTORY, 0);
    centrale.writeUInt16LE(voce.versioneCreatore, 4);
    centrale.writeUInt16LE(versioneNecessaria, 6);
    centrale.writeUInt16LE(flag, 8);
    centrale.writeUInt16LE(metodo, 10);
    centrale.writeUInt16LE(voce.ora, 12);
    centrale.writeUInt16LE(voce.data, 14);
    centrale.writeUInt32LE(crc, 16);
    centrale.writeUInt32LE(corpo.length, 20);
    centrale.writeUInt32LE(byteVeri, 24);
    centrale.writeUInt16LE(nome.length, 28);
    centrale.writeUInt16LE(0, 30);
    centrale.writeUInt16LE(0, 32);
    centrale.writeUInt16LE(0, 34);
    centrale.writeUInt16LE(voce.attributiInterni, 36);
    centrale.writeUInt32LE(voce.attributiEsterni, 38);
    centrale.writeUInt32LE(offset, 42);
    centrali.push(centrale, nome);

    offset += locale.length + nome.length + corpo.length;
  }
  const dimensioneCentrale = centrali.reduce((somma, pezzo) => somma + pezzo.length, 0);
  const coda = Buffer.alloc(22);
  coda.writeUInt32LE(FIRMA_FINE_DIRECTORY, 0);
  coda.writeUInt16LE(0, 4);
  coda.writeUInt16LE(0, 6);
  coda.writeUInt16LE(voci.length, 8);
  coda.writeUInt16LE(voci.length, 10);
  coda.writeUInt32LE(dimensioneCentrale, 12);
  coda.writeUInt32LE(offset, 16);
  coda.writeUInt16LE(0, 20);
  return Buffer.concat([...locali, ...centrali, coda]);
}

// ---------------------------------------------------------------------------
// Sheet XML
// ---------------------------------------------------------------------------

// Real files use bare tags (`<c>`, as Excel writes them); some other
// programs write them with a namespace prefix (`<x:c>`). Both are read;
// when writing, whichever prefix the sheet already uses is kept.
const PREFISSO = "(?:[A-Za-z_][\\w.-]*:)?";

export function decodificaXml(testo) {
  return String(testo)
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#x([0-9A-Fa-f]+);/g, (_tutto, esadecimale) => String.fromCodePoint(parseInt(esadecimale, 16)))
    .replace(/&#(\d+);/g, (_tutto, decimale) => String.fromCodePoint(Number(decimale)))
    .replace(/&amp;/g, "&");
}

/** Concatenated `<t>` text of a fragment; enough for `<si>` and `<is>`. */
function testoDeiTag(frammento) {
  const testo = new RegExp(`<${PREFISSO}t(?:\\s[^>]*)?(?:/>|>([\\s\\S]*?)</${PREFISSO}t>)`, "g");
  let contenuto = "";
  let pezzo;
  while ((pezzo = testo.exec(frammento)) !== null) contenuto += decodificaXml(pezzo[1] ?? "");
  return contenuto;
}

/** Shared strings, in the order cells index them by. */
function stringheCondivise(xml) {
  const elenco = [];
  if (!xml) return elenco;
  const voci = new RegExp(`<${PREFISSO}si(?:\\s[^>]*)?(?:/>|>([\\s\\S]*?)</${PREFISSO}si>)`, "g");
  let voce;
  while ((voce = voci.exec(xml)) !== null) elenco.push(testoDeiTag(voce[1] ?? ""));
  return elenco;
}

/** The workbook's sheets, in order, with the name of their XML part. */
export function partiDeiFogli(libro, relazioni) {
  const bersagli = new Map();
  const relazione = /<[^>]*\bId="([^"]+)"[^>]*\bTarget="([^"]+)"[^>]*>/g;
  const alternativa = /<[^>]*\bTarget="([^"]+)"[^>]*\bId="([^"]+)"[^>]*>/g;
  let trovata;
  while ((trovata = relazione.exec(relazioni ?? "")) !== null) bersagli.set(trovata[1], trovata[2]);
  while ((trovata = alternativa.exec(relazioni ?? "")) !== null) {
    if (!bersagli.has(trovata[2])) bersagli.set(trovata[2], trovata[1]);
  }

  const fogli = [];
  const elenco = new RegExp(`<${PREFISSO}sheet\\s[^>]*/?>`, "g");
  let voce;
  while ((voce = elenco.exec(libro ?? "")) !== null) {
    const tag = voce[0];
    const nome = /\bname="([^"]*)"/.exec(tag)?.[1];
    const riferimento = /\b(?:r:)?id="([^"]+)"/.exec(tag)?.[1];
    if (nome === undefined || !riferimento) continue;
    const bersaglio = bersagli.get(riferimento);
    if (!bersaglio) continue;
    const parte = bersaglio.startsWith("/")
      ? bersaglio.slice(1)
      : `xl/${bersaglio.replace(/^\.\//, "")}`;
    fogli.push({ nome: decodificaXml(nome), parte });
  }
  return fogli;
}

/** `A` becomes 1, `AB` becomes 28. */
export function numeroDiColonna(lettere) {
  let numero = 0;
  for (const carattere of String(lettere).toUpperCase()) numero = numero * 26 + (carattere.charCodeAt(0) - 64);
  return numero;
}

/** 1 becomes `A`, 28 becomes `AB`. */
export function letteraDiColonna(numero) {
  let resto = Number(numero);
  let lettera = "";
  while (resto > 0) {
    const modulo = (resto - 1) % 26;
    lettera = String.fromCharCode(65 + modulo) + lettera;
    resto = Math.floor((resto - modulo) / 26);
  }
  return lettera;
}

function spezzaIndirizzo(indirizzo) {
  const trovato = /^([A-Z]{1,3})(\d+)$/.exec(String(indirizzo).toUpperCase());
  if (!trovato) throw new FoglioNonScrivibile(`«${indirizzo}» non è l'indirizzo di una cella.`);
  return { colonna: numeroDiColonna(trovato[1]), riga: Number(trovato[2]) };
}

/**
 * A sheet: its XML, parsed once, with rows and cells kept as written.
 * Every row and cell retains its original text, and saving just
 * concatenates those texts back together, so an untouched row comes out
 * character-for-character identical to how it started.
 */
class Foglio {
  constructor(nome, parte, xml, condivise) {
    this.nome = nome;
    this.parte = parte;
    this.xml = xml;
    this.condivise = condivise;
    this.modificato = false;
    this.prefisso = "";
    this.righe = [];
    this._leggi();
  }

  _leggi() {
    const apertura = new RegExp(`<(${PREFISSO})sheetData(?:\\s[^>]*)?(/?)>`).exec(this.xml);
    if (!apertura) throw new FoglioNonScrivibile(`Il foglio «${this.nome}» non ha l'elenco delle righe.`);
    this.prefisso = apertura[1] ?? "";
    const p = this.prefisso;
    if (apertura[2] === "/") {
      // `<sheetData/>`: no rows. Reopened as a tag pair so a write has
      // somewhere to insert a row.
      const inizio = apertura.index;
      this.xml = `${this.xml.slice(0, inizio)}<${p}sheetData></${p}sheetData>${this.xml.slice(inizio + apertura[0].length)}`;
      this.inizioRighe = inizio + `<${p}sheetData>`.length;
      this.fineRighe = this.inizioRighe;
      return;
    }
    this.inizioRighe = apertura.index + apertura[0].length;
    const chiusura = this.xml.indexOf(`</${p}sheetData>`, this.inizioRighe);
    if (chiusura < 0) throw new FoglioNonScrivibile(`Il foglio «${this.nome}» ha l'elenco delle righe aperto e mai chiuso.`);
    this.fineRighe = chiusura;
    const corpo = this.xml.slice(this.inizioRighe, this.fineRighe);
    const riga = new RegExp(`<${p}row\\b([^>]*?)(?:/>|>([\\s\\S]*?)</${p}row>)`, "g");
    let trovata;
    while ((trovata = riga.exec(corpo)) !== null) {
      const numero = Number(/\br="(\d+)"/.exec(trovata[1])?.[1]);
      if (!Number.isInteger(numero)) throw new FoglioNonScrivibile(`Il foglio «${this.nome}» ha una riga senza numero.`);
      this.righe.push({
        numero,
        attributi: trovata[1],
        celle: this._celle(trovata[2] ?? ""),
        testo: trovata[0],
        intatta: true,
      });
    }
  }

  _celle(corpo) {
    const p = this.prefisso;
    const cella = new RegExp(`<${p}c\\b([^>]*?)(?:/>|>([\\s\\S]*?)</${p}c>)`, "g");
    const celle = [];
    let trovata;
    while ((trovata = cella.exec(corpo)) !== null) {
      const indirizzo = /\br="([A-Z]{1,3}\d+)"/.exec(trovata[1])?.[1];
      if (!indirizzo) continue;
      celle.push({
        colonna: spezzaIndirizzo(indirizzo).colonna,
        attributi: trovata[1],
        interno: trovata[2] ?? "",
        testo: trovata[0],
      });
    }
    return celle;
  }

  _riga(numero) {
    return this.righe.find((riga) => riga.numero === numero) ?? null;
  }

  /** What's stored in a cell, as it would display to whoever opens the sheet. */
  valore(indirizzo) {
    const { colonna, riga } = spezzaIndirizzo(indirizzo);
    const cella = this._riga(riga)?.celle.find((voce) => voce.colonna === colonna);
    return cella ? this._valoreDi(cella) : null;
  }

  _valoreDi(cella) {
    const p = this.prefisso;
    const tipo = /\bt="([^"]*)"/.exec(cella.attributi)?.[1] ?? "n";
    if (tipo === "inlineStr") return testoDeiTag(cella.interno);
    const grezzo = new RegExp(`<${p}v(?:\\s[^>]*)?>([\\s\\S]*?)</${p}v>`).exec(cella.interno)?.[1];
    if (grezzo === undefined) return null;
    if (tipo === "s") {
      const indice = Number.parseInt(grezzo, 10);
      return Number.isInteger(indice) && indice < this.condivise.length ? this.condivise[indice] : "";
    }
    if (tipo === "b") return grezzo.trim() === "1";
    if (tipo === "str" || tipo === "e" || tipo === "d") return decodificaXml(grezzo);
    const numero = Number(grezzo);
    return Number.isFinite(numero) ? numero : decodificaXml(grezzo);
  }

  /**
   * The value grid, from cell A1: `values[row - 1][column - 1]`. `rowIndex`
   * and `columnIndex` are always zero; they exist because `write_supplier_orders.mjs`
   * already does that arithmetic against a used-range shape.
   */
  griglia() {
    let ultimaRiga = 0;
    let ultimaColonna = 0;
    for (const riga of this.righe) {
      for (const cella of riga.celle) {
        if (this._valoreDi(cella) === null) continue;
        if (riga.numero > ultimaRiga) ultimaRiga = riga.numero;
        if (cella.colonna > ultimaColonna) ultimaColonna = cella.colonna;
      }
    }
    if (ultimaRiga * ultimaColonna > MASSIME_CELLE_IN_GRIGLIA) {
      throw new FoglioNonScrivibile(
        `Il foglio «${this.nome}» dichiara celle fino alla riga ${ultimaRiga} e alla colonna ` +
          `${letteraDiColonna(ultimaColonna)}: non è la forma di un listino.`,
      );
    }
    const values = [];
    for (let numero = 1; numero <= ultimaRiga; numero += 1) values.push(new Array(ultimaColonna).fill(null));
    for (const riga of this.righe) {
      if (riga.numero > ultimaRiga) continue;
      for (const cella of riga.celle) {
        if (cella.colonna > ultimaColonna) continue;
        values[riga.numero - 1][cella.colonna - 1] = this._valoreDi(cella);
      }
    }
    return { rowIndex: 0, columnIndex: 0, rowCount: ultimaRiga, columnCount: ultimaColonna, values };
  }

  _stile(cella) {
    return /\bs="(\d+)"/.exec(cella.attributi)?.[1] ?? null;
  }

  _rifiutaSeFormula(cella, indirizzo) {
    if (new RegExp(`<${this.prefisso}f\\b`).test(cella.interno)) {
      throw new FoglioNonScrivibile(
        `La cella ${indirizzo} del foglio «${this.nome}» contiene una formula del fornitore: ` +
          "non ci si scrive sopra.",
      );
    }
  }

  _sostituisci(riga, colonna, testoCella, stile) {
    const p = this.prefisso;
    let destinazione = this._riga(riga);
    if (!destinazione) {
      destinazione = { numero: riga, attributi: ` r="${riga}"`, celle: [], testo: "", intatta: false };
      const dopo = this.righe.findIndex((voce) => voce.numero > riga);
      if (dopo < 0) this.righe.push(destinazione);
      else this.righe.splice(dopo, 0, destinazione);
    }
    const indirizzo = `${letteraDiColonna(colonna)}${riga}`;
    const esistente = destinazione.celle.find((voce) => voce.colonna === colonna);
    const attributi = ` r="${indirizzo}"${stile !== null ? ` s="${stile}"` : ""}`;
    const testo = testoCella(attributi, p);
    if (esistente) {
      esistente.attributi = attributi;
      esistente.interno = "";
      esistente.testo = testo;
    } else {
      const nuova = { colonna, attributi, interno: "", testo };
      const dopo = destinazione.celle.findIndex((voce) => voce.colonna > colonna);
      if (dopo < 0) destinazione.celle.push(nuova);
      else destinazione.celle.splice(dopo, 0, nuova);
    }
    destinazione.intatta = false;
    this.modificato = true;
  }

  /** Writes a number into the cell: the only thing an order quantity is. */
  scriviNumero(indirizzo, numero) {
    if (typeof numero !== "number" || !Number.isFinite(numero)) {
      throw new FoglioNonScrivibile(`In ${indirizzo} si voleva scrivere «${numero}», che non è un numero.`);
    }
    const { colonna, riga } = spezzaIndirizzo(indirizzo);
    const esistente = this._riga(riga)?.celle.find((voce) => voce.colonna === colonna);
    if (esistente) this._rifiutaSeFormula(esistente, indirizzo);
    // An existing cell keeps its own style; a new cell gets none. Inheriting
    // one from the column (`<cols>`) or the nearest cell looks tempting, but
    // a column-wide "text" format would then apply to the new cell and the
    // number would no longer display as a number, which the cell-by-cell
    // guard correctly rejects.
    const stile = esistente ? this._stile(esistente) : null;
    this._sostituisci(riga, colonna, (attributi, p) => `<${p}c${attributi}><${p}v>${numero}</${p}v></${p}c>`, stile);
  }

  /**
   * Clears a column's cells between two rows, leaving the style in place —
   * the same effect as Excel's "Clear Contents". Cells that don't exist
   * stay nonexistent. Returns how many cells were cleared.
   */
  svuota(lettera, primaRiga, ultimaRiga) {
    const colonna = numeroDiColonna(lettera);
    let svuotate = 0;
    for (const riga of this.righe) {
      if (riga.numero < primaRiga || riga.numero > ultimaRiga) continue;
      const cella = riga.celle.find((voce) => voce.colonna === colonna);
      if (!cella || this._valoreDi(cella) === null) continue;
      const indirizzo = `${letteraDiColonna(colonna)}${riga.numero}`;
      this._rifiutaSeFormula(cella, indirizzo);
      this._sostituisci(riga.numero, colonna, (attributi, p) => `<${p}c${attributi}/>`, this._stile(cella));
      svuotate += 1;
    }
    return svuotate;
  }

  /**
   * Stops everything if that column, between two rows, contains a formula —
   * even on a row that isn't being written, and even one with no cached
   * result stored.
   *
   * This has to be a separate pass: `scriviNumero` and `svuota` only reject
   * formulas on cells they actually touch, and `svuota` only looks at cells
   * that have a value. A programmatically generated formula often has no
   * cached value (unlike one saved by Excel itself), so on a row nobody is
   * ordering, neither of those would catch it — it would survive into the
   * copy unchanged, the cell-by-cell comparison would see formula against
   * formula and find no difference, and Excel would evaluate it on open,
   * shipping the supplier a quantity nobody requested.
   */
  rifiutaFormuleNellaColonna(lettera, primaRiga, ultimaRiga) {
    const colonna = numeroDiColonna(lettera);
    const formula = new RegExp(`<${this.prefisso}f\\b`);
    for (const riga of this.righe) {
      if (riga.numero < primaRiga || riga.numero > ultimaRiga) continue;
      const cella = riga.celle.find((voce) => voce.colonna === colonna);
      if (!cella || !formula.test(cella.interno)) continue;
      throw new FoglioNonScrivibile(
        `La cella ${letteraDiColonna(colonna)}${riga.numero} del foglio «${this.nome}» porta una ` +
          "formula nella colonna d'ordine: Excel la ricalcolerebbe all'apertura, e nell'ordine " +
          "finirebbero colli che nessuno ha chiesto.",
      );
    }
  }

  /** Does the sheet have at least one formula? Used to ask Excel to recalculate. */
  haFormule() {
    return new RegExp(`<${this.prefisso}f\\b`).test(this.xml);
  }

  /** The sheet's XML to save: identical to before wherever nothing was touched. */
  xmlDaSalvare() {
    if (!this.modificato) return this.xml;
    const p = this.prefisso;
    const corpo = this.righe.map((riga) => {
      if (riga.intatta) return riga.testo;
      return `<${p}row${riga.attributi}>${riga.celle.map((cella) => cella.testo).join("")}</${p}row>`;
    }).join("");
    let xml = this.xml.slice(0, this.inizioRighe) + corpo + this.xml.slice(this.fineRighe);
    return this._conDimensioneAggiornata(xml);
  }

  /**
   * `<dimension ref="A1:R7070"/>` declares how far the cells extend. A cell
   * written outside that rectangle expands it: Excel tolerates a wrong
   * value here, but a stricter reader might refuse the file.
   */
  _conDimensioneAggiornata(xml) {
    const tag = new RegExp(`<${this.prefisso}dimension\\s+ref="([A-Z]{1,3}\\d+)(?::([A-Z]{1,3}\\d+))?"\\s*/>`).exec(xml);
    if (!tag) return xml;
    const da = spezzaIndirizzo(tag[1]);
    const a = spezzaIndirizzo(tag[2] ?? tag[1]);
    let { colonna: minColonna, riga: minRiga } = da;
    let { colonna: maxColonna, riga: maxRiga } = a;
    for (const riga of this.righe) {
      if (riga.intatta) continue;
      for (const cella of riga.celle) {
        minRiga = Math.min(minRiga, riga.numero);
        maxRiga = Math.max(maxRiga, riga.numero);
        minColonna = Math.min(minColonna, cella.colonna);
        maxColonna = Math.max(maxColonna, cella.colonna);
      }
    }
    const nuovo = `${letteraDiColonna(minColonna)}${minRiga}:${letteraDiColonna(maxColonna)}${maxRiga}`;
    const vecchio = tag[2] ? `${tag[1]}:${tag[2]}` : tag[1];
    if (nuovo === vecchio) return xml;
    return xml.slice(0, tag.index) + `<${this.prefisso}dimension ref="${nuovo}"/>` + xml.slice(tag.index + tag[0].length);
  }
}

// ---------------------------------------------------------------------------
// The workbook
// ---------------------------------------------------------------------------

class Libro {
  constructor(dati, voci) {
    this.dati = dati;
    this.voci = voci;
    this.perNome = new Map(voci.map((voce) => [voce.nome, voce]));
    this.fogliAperti = new Map();
    const libro = this._parte("xl/workbook.xml");
    if (libro === null) throw new ContenitoreNonLeggibile("Il documento non contiene un libro di Excel (manca xl/workbook.xml).");
    this.libroXml = libro;
    this.fogli = partiDeiFogli(libro, this._parte("xl/_rels/workbook.xml.rels"));
    this.condivise = stringheCondivise(this._parte("xl/sharedStrings.xml"));
  }

  _parte(nome) {
    const voce = this.perNome.get(nome);
    return voce ? leggiParte(this.dati, voce) : null;
  }

  /** Sheet names, in workbook order. */
  get nomiDeiFogli() {
    return this.fogli.map((foglio) => foglio.nome);
  }

  /** The sheet with that name, or the first one for "FIRST"; `null` if none matches. */
  foglio(nome) {
    const cercato = String(nome ?? "").trim();
    const scelto = cercato.toUpperCase() === "FIRST"
      ? this.fogli[0]
      : this.fogli.find((foglio) => foglio.nome === cercato);
    if (!scelto) return null;
    if (!this.fogliAperti.has(scelto.parte)) {
      const xml = this._parte(scelto.parte);
      if (xml === null) throw new ContenitoreNonLeggibile(`La parte del foglio «${scelto.nome}» manca dal documento.`);
      this.fogliAperti.set(scelto.parte, new Foglio(scelto.nome, scelto.parte, xml, this.condivise));
    }
    return this.fogliAperti.get(scelto.parte);
  }

  /**
   * `workbook.xml` with `fullCalcOnLoad` set, if a touched sheet has
   * formulas: the totals the supplier computes on the order column get
   * recalculated by Excel on open, since nothing is recalculated here.
   * If `calcPr` doesn't exist yet, it's inserted where the schema expects
   * it — after the sheets and defined names — because an out-of-place
   * element is exactly what makes Excel offer to "repair" the file.
   */
  _libroDaSalvare() {
    const toccatiConFormule = [...this.fogliAperti.values()].some((foglio) => foglio.modificato && foglio.haFormule());
    if (!toccatiConFormule) return null;
    const xml = this.libroXml;
    const prefisso = new RegExp(`<(${PREFISSO})workbook\\b`).exec(xml)?.[1] ?? "";
    const calcPr = new RegExp(`<${prefisso}calcPr\\b([^>]*?)(/?)>`).exec(xml);
    if (calcPr) {
      if (/\bfullCalcOnLoad="(1|true)"/.test(calcPr[1])) return null;
      const attributi = calcPr[1].replace(/\s*\bfullCalcOnLoad="[^"]*"/, "");
      return xml.slice(0, calcPr.index) + `<${prefisso}calcPr${attributi} fullCalcOnLoad="1"${calcPr[2]}>` + xml.slice(calcPr.index + calcPr[0].length);
    }
    let posizione = -1;
    for (const tag of ["sheets", "functionGroups", "externalReferences", "definedNames"]) {
      const chiuso = xml.lastIndexOf(`</${prefisso}${tag}>`);
      const vuoto = new RegExp(`<${prefisso}${tag}\\b[^>]*/>`).exec(xml);
      const fine = Math.max(chiuso >= 0 ? chiuso + `</${prefisso}${tag}>`.length : -1, vuoto ? vuoto.index + vuoto[0].length : -1);
      if (fine > posizione) posizione = fine;
    }
    if (posizione < 0) return null;
    return `${xml.slice(0, posizione)}<${prefisso}calcPr fullCalcOnLoad="1"/>${xml.slice(posizione)}`;
  }

  /** Writes the copy: every part as it was, except the touched sheets. */
  async salva(percorso) {
    const sostituite = new Map();
    for (const foglio of this.fogliAperti.values()) {
      if (foglio.modificato) sostituite.set(foglio.parte, foglio.xmlDaSalvare());
    }
    const libro = this._libroDaSalvare();
    if (libro !== null) sostituite.set("xl/workbook.xml", libro);
    await writeFile(percorso, costruisciZip(this.dati, this.voci, sostituite));
    return sostituite.size;
  }
}

/** Opens a `.xlsx` from disk, read-only: the source file is never touched. */
export async function apriLibro(percorso) {
  const dati = await readFile(percorso);
  return new Libro(dati, elencoVoci(dati));
}

export const _perLeProve = { Foglio, Libro, costruisciZip, stringheCondivise, testoDeiTag };
