// Scrive le quantita' d'ordine dentro una copia di un `.xlsx` toccando SOLO il
// foglio dell'ordine, e di quel foglio solo le celle della colonna d'ordine.
//
// PERCHE' ESISTE QUESTO MODULO
// Fino al 5 settembre 2026 le copie le scriveva `@oai/artifact-tool`, una
// libreria .NET compilata in WebAssembly da 24 MB che importava il documento,
// se lo ricostruiva in memoria e lo riscriveva **da capo**.  Quello che perdeva
// per strada usciva lo stesso dalla porta: 34 celle EAN con scritto «1235» al
// posto del nulla, 641 titoli di sezione spariti (12 agosto), e una copia che
// Excel, riaperta, contestava (4 settembre).  La guardia cella per cella di
// `app/copia_fedele.py` prova i **valori**, non la **forma** del file.
//
// Qui non si ricostruisce niente.  Un `.xlsx` e' uno ZIP con dentro dell'XML:
// si apre lo ZIP con `zlib` e basta, si cambia il testo delle sole celle da
// scrivere dentro il foglio dell'ordine, e si richiude lo ZIP copiando ogni
// altra parte **byte per byte** com'era — stili, formule, disegni, celle unite,
// nomi definiti, tutto.  Il documento resta quello del fornitore, con la sola
// colonna d'ordine riempita: e' lo stesso principio della patch in posizione
// del `.xls` di Noce (`app/xls_writer.py`).
//
// Qui dentro non entra nessuna dipendenza: solo `node:zlib` e `node:fs`.
//
// Che cosa NON fa, e di proposito:
// - non scrive testo: una quantita' e' un numero, e il numero non ha bisogno
//   di `sharedStrings.xml`;
// - non tocca una cella che porta una formula: la ferma e lo dice, perche'
//   sovrascrivere una formula del fornitore e' proprio la cosa da non fare;
// - non ricalcola i totali: se il foglio ha formule, chiede a Excel di
//   ricalcolare all'apertura (`fullCalcOnLoad` in `workbook.xml`).

import { readFile, writeFile } from "node:fs/promises";
import { deflateRawSync, inflateRawSync } from "node:zlib";

const FIRMA_FINE_DIRECTORY = 0x06054b50;
const FIRMA_VOCE_DIRECTORY = 0x02014b50;
const FIRMA_INTESTAZIONE_LOCALE = 0x04034b50;
// Il commento finale di uno ZIP sta in due byte: al massimo 65.535 caratteri,
// piu' i 22 dell'intestazione di coda.
const MASSIMO_COMMENTO = 65535 + 22;
const METODO_DEFLATE = 8;
const METODO_NESSUNO = 0;
// Il bit 3 dei flag dice «le dimensioni stanno in un descrittore dopo i dati»:
// le intestazioni le riscriviamo noi con le dimensioni dentro, quindi va spento.
const FLAG_DESCRITTORE = 0x0008;
// Oltre questo numero di celle la griglia non si costruisce: un foglio che
// dichiara una cella a XFD1048576 non e' un listino, e' un file rovinato.
const MASSIME_CELLE_IN_GRIGLIA = 50_000_000;

export class ContenitoreNonLeggibile extends Error {}
export class FoglioNonScrivibile extends Error {}

// ---------------------------------------------------------------------------
// ZIP: lettura
// ---------------------------------------------------------------------------

/** Dove comincia l'intestazione di coda dello ZIP. */
function fineDirectory(dati) {
  const minimo = Math.max(0, dati.length - MASSIMO_COMMENTO);
  for (let posizione = dati.length - 22; posizione >= minimo; posizione -= 1) {
    if (dati.readUInt32LE(posizione) === FIRMA_FINE_DIRECTORY) return posizione;
  }
  throw new ContenitoreNonLeggibile("Il documento non è un contenitore ZIP valido.");
}

/**
 * Le voci dell'elenco centrale, nell'ordine in cui stanno nel file.
 *
 * Si tiene tutto quello che serve per **riscrivere** le intestazioni: chi
 * legge soltanto ne usa la meta'.
 */
export function elencoVoci(dati) {
  const fine = fineDirectory(dati);
  const quante = dati.readUInt16LE(fine + 10);
  const dimensione = dati.readUInt32LE(fine + 12);
  const inizio = dati.readUInt32LE(fine + 16);
  if (quante === 0xffff || dimensione === 0xffffffff || inizio === 0xffffffff) {
    // Un `.xlsx` di un listino non arriva mai a 4 GB o 65.535 parti: se ci
    // arriva, meglio dirlo che leggere numeri sbagliati.
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

/** I byte compressi di una voce, cosi' come stanno nel file. */
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

/** Il contenuto di una parte, gia' decompresso, come testo UTF-8. */
export function leggiParte(dati, voce) {
  const crudo = byteDellaVoce(dati, voce);
  if (voce.metodo === METODO_NESSUNO) return crudo.toString("utf8");
  if (voce.metodo === METODO_DEFLATE) return inflateRawSync(crudo).toString("utf8");
  throw new ContenitoreNonLeggibile(
    `La parte «${voce.nome}» del documento è compressa in un modo che non si sa leggere.`,
  );
}

// ---------------------------------------------------------------------------
// ZIP: scrittura
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

/** Il CRC-32 dello ZIP, calcolato a mano: `zlib.crc32` esiste solo da Node 22. */
export function crc32(dati) {
  let crc = 0xffffffff;
  for (let i = 0; i < dati.length; i += 1) crc = TABELLA_CRC[(crc ^ dati[i]) & 0xff] ^ (crc >>> 8);
  return (crc ^ 0xffffffff) >>> 0;
}

/**
 * Lo ZIP riscritto: ogni parte copiata **compressa com'era**, tranne quelle
 * sostituite.  Le intestazioni si riscrivono tutte, senza campi extra e senza
 * descrittori: e' la forma piu' semplice che ogni lettore accetta, e i byte
 * dei dati restano quelli del fornitore.
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
// L'XML dei fogli
// ---------------------------------------------------------------------------

// Nei file veri i tag arrivano nudi (`<c>`, come li scrive Excel); qualche
// altro programma li scrive con un prefisso (`<x:c>`).  Si leggono tutti e due;
// scrivendo, si usa il prefisso che il foglio usa gia'.
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

/** I testi `<t>` di un frammento, concatenati: basta per `<si>` e `<is>`. */
function testoDeiTag(frammento) {
  const testo = new RegExp(`<${PREFISSO}t(?:\\s[^>]*)?(?:/>|>([\\s\\S]*?)</${PREFISSO}t>)`, "g");
  let contenuto = "";
  let pezzo;
  while ((pezzo = testo.exec(frammento)) !== null) contenuto += decodificaXml(pezzo[1] ?? "");
  return contenuto;
}

/** Le stringhe condivise, nell'ordine in cui le indicizzano le celle. */
function stringheCondivise(xml) {
  const elenco = [];
  if (!xml) return elenco;
  const voci = new RegExp(`<${PREFISSO}si(?:\\s[^>]*)?(?:/>|>([\\s\\S]*?)</${PREFISSO}si>)`, "g");
  let voce;
  while ((voce = voci.exec(xml)) !== null) elenco.push(testoDeiTag(voce[1] ?? ""));
  return elenco;
}

/** I fogli del libro, in ordine, con il nome della loro parte XML. */
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

/** `A` diventa 1, `AB` diventa 28. */
export function numeroDiColonna(lettere) {
  let numero = 0;
  for (const carattere of String(lettere).toUpperCase()) numero = numero * 26 + (carattere.charCodeAt(0) - 64);
  return numero;
}

/** 1 diventa `A`, 28 diventa `AB`. */
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
 * Un foglio: il suo XML, letto una volta, con le righe e le celle come stanno
 * scritte.  Ogni riga e ogni cella conserva il proprio **testo originale**, e
 * quando si salva si rimettono in fila quei testi: una riga che non si e'
 * toccata esce identica a com'era, carattere per carattere.
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
      // `<sheetData/>`: nessuna riga.  Si riapre come coppia di tag, cosi' chi
      // scrive una riga ha dove metterla.
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

  /** Quello che c'e' scritto in una cella, come lo vedrebbe chi apre il foglio. */
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
   * La griglia dei valori, dalla cella A1: `values[riga - 1][colonna - 1]`.
   * `rowIndex` e `columnIndex` valgono zero e ci sono perche' chi la legge
   * (`write_supplier_orders.mjs`) faceva gia' quel conto sull'intervallo
   * usato della libreria di prima.
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

  /** Scrive un numero nella cella: l'unica cosa che una quantita' d'ordine e'. */
  scriviNumero(indirizzo, numero) {
    if (typeof numero !== "number" || !Number.isFinite(numero)) {
      throw new FoglioNonScrivibile(`In ${indirizzo} si voleva scrivere «${numero}», che non è un numero.`);
    }
    const { colonna, riga } = spezzaIndirizzo(indirizzo);
    const esistente = this._riga(riga)?.celle.find((voce) => voce.colonna === colonna);
    if (esistente) this._rifiutaSeFormula(esistente, indirizzo);
    // Una cella che esiste tiene il suo stile; una cella nuova non ne prende
    // nessuno.  ⚠ Ereditarlo dalla colonna (`<cols>`) o dalla cella piu'
    // vicina sembrava piu' furbo, e su `documenti/prova2.xlsx` portava nella
    // cella nuova un formato «testo» dichiarato per tutte le colonne: la
    // guardia cella per cella rifiutava la copia, e giustamente, perche' il
    // numero non si sarebbe piu' mostrato come numero.
    const stile = esistente ? this._stile(esistente) : null;
    this._sostituisci(riga, colonna, (attributi, p) => `<${p}c${attributi}><${p}v>${numero}</${p}v></${p}c>`, stile);
  }

  /**
   * Svuota le celle di una colonna fra due righe, **lasciando lo stile**: e'
   * quello che fa Excel con «Cancella contenuto».  Le celle che non esistono
   * restano inesistenti.  Torna quante ne ha svuotate.
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
   * Ferma tutto se in quella colonna, fra due righe, c'e' una formula — anche
   * su una riga che non si scrive e anche senza il risultato memorizzato.
   *
   * ⚠ 6 settembre 2026: il «no» alle formule lo dicevano solo `scriviNumero` e
   * `svuota`, cioe' le celle che si toccano, e `svuota` guarda le celle che un
   * valore ce l'hanno.  Una formula generata da programma il valore in cache
   * non ce l'ha — un `.xlsx` salvato da Excel si', quello del gestionale di un
   * fornitore no — quindi su una riga che nessuno ordina non la prendeva in
   * mano nessuno: restava nella copia, il confronto cella per cella vedeva
   * formula contro formula e non trovava differenze, ed Excel all'apertura la
   * calcolava.  Al fornitore arrivavano colli che nessuno ha chiesto.
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

  /** Il foglio ha almeno una formula? Serve a chiedere a Excel di ricalcolare. */
  haFormule() {
    return new RegExp(`<${this.prefisso}f\\b`).test(this.xml);
  }

  /** L'XML del foglio da salvare: identico a prima dove non si e' toccato. */
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
   * `<dimension ref="A1:R7070"/>` dice fin dove arrivano le celle.  Una cella
   * scritta fuori da quel rettangolo lo allarga: Excel lo tollera anche
   * sbagliato, ma un lettore prudente potrebbe fermarsi prima.
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
// Il libro
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

  /** I nomi dei fogli, nell'ordine del libro. */
  get nomiDeiFogli() {
    return this.fogli.map((foglio) => foglio.nome);
  }

  /** Il foglio con quel nome, o il primo per «FIRST»; `null` se non c'e'. */
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
   * `workbook.xml` con `fullCalcOnLoad`, se un foglio toccato ha formule: i
   * totali che il fornitore calcola sulla colonna d'ordine li ricalcola Excel
   * all'apertura, perche' qui non si ricalcola niente.  Se `calcPr` non c'e' lo
   * si mette dove lo schema lo vuole — dopo i fogli e i nomi definiti — perche'
   * un elemento fuori posto e' proprio il tipo di cosa che fa chiedere a Excel
   * di «riparare» il file.
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

  /** Scrive la copia: ogni parte com'era, tranne i fogli toccati. */
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

/** Apre un `.xlsx` dal disco, in sola lettura: il file di partenza non si tocca mai. */
export async function apriLibro(percorso) {
  const dati = await readFile(percorso);
  return new Libro(dati, elencoVoci(dati));
}

export const _perLeProve = { Foglio, Libro, costruisciZip, stringheCondivise, testoDeiTag };
