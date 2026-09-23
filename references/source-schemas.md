# Schemi delle fonti

## Indice

1. Regole comuni
2. Export gestionale
3. BETULLA
4. Larice
4-bis. Larice — il canvass nuovo
5. Espositori Larice
6. Cipresso
7. Noce CSV
8. Noce listino .xls
9. Scrittura degli ordini

## 1. Regole comuni

- Trattare gli input come read-only.
- Il formato di un file lo dicono i suoi primi byte, non l'estensione: `D0 CF 11 E0` è un Excel 97-2003 e si legge con `app/xls_reader.py`, `PK\x03\x04` è un Excel 2007 o successivo e si legge con openpyxl, tutto il resto passa dal lettore CSV. Un listino rinominato non deve produrre un errore incomprensibile.
- Leggere l'intero foglio attivo; non usare come limite il range di una tabella, un filtro o un'anteprima.
- Considerare validi soltanto valori collegati a celle attive. Non cercare EAN direttamente nei file XML interni o in `sharedStrings.xml`.
- Conservare EAN e codici articolo come testo, inclusi zeri iniziali e codici non standard.
- Non deduplicare un listino usando soltanto l'EAN.

## 2. Export gestionale

Firma attesa nel foglio `Foglio1`:

- riga documento iniziale con tipo `T`;
- riga intestazioni contenente `Codice`, `Descrizione`, `Colli`, `Quantità`, `Prezzo`, `Sconto`, `IVA`, `Totale`;
- righe prodotto con tipo `C` in colonna A.

Mappatura:

| Campo | Colonna |
| --- | --- |
| EAN source of truth | B `Codice` |
| Descrizione | D |
| UM | E |
| Colli esportati | F, riusata come quantità predefinita in colli (`suggested_colli` → `product.suggestedQuantity`) |
| Quantità esportata | G, da ignorare per il nuovo ordine |
| Ultimo prezzo netto pagato | H `Prezzo` |
| Sconto informativo | I |
| IVA | J |
| Totale storico | K |

Nel comparatore HTML l'utente inserisce direttamente il numero di colli da ordinare: non c'è arrotondamento e non esiste eccedenza. La colonna `Colli` (F) non va più ignorata: il suo valore precompila il campo quantità di ogni prodotto (`quantitySource: "gestionale"`); se l'utente lo modifica il prodotto passa a `quantitySource: "utente"`. La colonna `Quantità` (G) resta storica e continua a non essere riusata. Per ciascuna offerta il totale riga è `totale = colli * prezzo per collo` (equivalente a `colli * pezzi per collo * prezzo per pezzo`). Nel vecchio comparativo Excel opzionale, `Colli da ordinare` resta invece una colonna manuale vuota.

## 3. BETULLA

Firma attesa: `EAN`, `CodArt`, `ORDINE`, `Descr.Commerciale`, `PzCt`, `Cessione`, `Pedana`, `Iva`, `TOTALI`.

| Campo | Colonna |
| --- | --- |
| EAN | A |
| Codice articolo | B |
| Quantità/colli d'ordine | C |
| Descrizione | D |
| Pezzi per cartone | E |
| Prezzo unitario netto | F `Cessione` |
| Pedana | G |
| IVA | H |
| Totale riga | I |

La formula totale riga è `ORDINE * PzCt * Cessione`. La riga finale contiene il totale generale.

## 4. Larice

Il nome del foglio cambia con numero e date del canvass. Usare il primo foglio commerciale e validarne il profilo delle colonne; non codificare il nome della run corrente. Il foglio può superare di molto il range della tabella formattata. Scansionare fino all'ultima riga attiva del worksheet.

| Campo | Colonna |
| --- | --- |
| Famiglia/promozione | A, spesso nascosta |
| Indicatore | B |
| Codice articolo | C |
| Quantità/colli d'ordine | D |
| Pezzi per cartone | E |
| Pedana | F |
| Descrizione | G |
| Annotazione prezzo precedente | M |
| Prezzo pre-sconto | O |
| Percentuale o codice sconto | P |
| IVA | Q |
| EAN | R |

Calcolare il prezzo post-sconto deterministicamente:

```text
se P è numerico: prezzo_post = O * (1 - P)
se P è testuale: prezzo_post = O
```

I valori numerici sono percentuali Excel, per esempio `0,10 = 10%`. `TP` e `SM` significano nessuno sconto. Applicare la stessa regola a ogni altro valore testuale, ma segnalare come anomalo un codice testuale diverso da quelli conosciuti; nel file di riferimento esistono anche poche righe con `**`.

Confrontare e totalizzare usando il prezzo post-sconto. Conservare nel comparativo anche prezzo pre-sconto e valore di P per audit.

## 4-bis. Larice — il canvass nuovo (`larice_canvass_v1`)

⚠ Dal **4 settembre 2026** Larice manda un secondo canvass, con uno schema che con quello del §4 non ha niente in comune: `New Larice N°37(v.0)`, intestato «Larice Ingrosso Srl». Il §4 **non si tocca**: il fornitore può tornare al formato di prima da una settimana all'altra, e i due adattatori convivono sullo stesso `supplier_id`.

Le due firme si escludono a vicenda apposta: il §4 pretende almeno 18 colonne (qui sono 16), questo pretende le sue intestazioni (là non ce n'è nessuna). Le intestazioni stanno alla **riga 11**, i dati dalla **12**; sopra c'è il blocco con i dati del fornitore, quelli del cliente e i quattro totali.

| Campo | Colonna | Intestazione |
| --- | --- | --- |
| EAN | B | `CODICE EAN` |
| Codice articolo | C | `CODICE` |
| Quantità/colli d'ordine | D | `QTA` |
| Descrizione | E | `DESCRIZIONE ARTICOLO` |
| Marcatore di riga | F | _nessuna_ (`PROMO`, `NOVITA`) |
| IVA | G | `IVA` |
| Pezzi per cartone | H | `IMB.` |
| Prezzo pre-sconto | I | `LISTINO` |
| Percentuale di sconto | J | `SCONTO` |
| **Prezzo post-sconto** | **K** | `NETTO` |

⚠ **Il prezzo su cui si confronta è NETTO (K), e si legge così com'è.** A differenza del §4 non si calcola niente: la percentuale di J il fornitore l'ha già applicata dentro K. Dichiarare `unit_price_pre_discount` su questo schema accenderebbe il ramo che ricalcola lo sconto, e lo applicherebbe una seconda volta.

**Le righe premio non sono marcate.** La colonna F dice `PROMO` su tutta la merce in promozione, premio compreso: la riga «IN OMAGGIO 1CT …» si riconosce solo dal testo, e va dichiarata con `row_markers.reward_rows`. Misurato sul listino del 4 settembre 2026: sei righe premio su 6.537, zero falsi positivi, e **cinque delle sei ripetono l'EAN di un articolo già a listino a prezzo pieno** — l'EAN `8019580330416` sta a 0,68 come merce e a 0,65 come regalo.

**Le condizioni commerciali** stanno disposte a blocchi come nel §4 — intestazione con la quantità, merce ammessa, riga dell'omaggio — ma tutte nella **stessa colonna E**, nome del premio compreso: `commercial_conditions.fields` dichiara `description` sia per `text` sia per `reward`. Sei soglie su sei, misurate.

**Espositori**: in questo schema non c'è nessuna struttura padre/componenti (H e I sono numeriche), e il §5 non si applica. Se un canvass nuovo ne porterà uno, le colonne si dichiarano con quel documento davanti.

## 5. Espositori Larice

Un espositore è un'offerta composta ordinabile sulla riga padre. Non modellarlo come una sequenza di prodotti autonomi.

Riga padre tipica:

- etichetta promozione/espositore in A;
- indicatore e codice articolo in B/C;
- quantità ordine in D e unità commerciale in E;
- descrizione padre in G;
- prezzo e sconto in O/P;
- EAN padre spesso assente o non identificativo in R.

Righe componente tipiche:

- stessa etichetta/blocco in A;
- codice articolo fornitore C assente;
- quantità componente in H e descrizione in I;
- prezzo componente in O e sconto in P;
- EAN componente in R spesso presente.

L'assenza riguarda quindi il **codice fornitore**, non necessariamente l'EAN. Usare congiuntamente semantica del nome, struttura padre/figli, continuità del blocco, quantità e prezzi. Rifiutare negazioni come `NO ESPO` e falsi positivi lessicali come `ESPRESSO`.

Riconciliazioni:

```text
quantità dichiarata padre = somma quantità componenti
prezzo lordo padre = somma(quantità componente * prezzo lordo componente)
prezzo netto espositore = prezzo lordo padre * (1 - sconto numerico)
```

Tollerare arrotondamenti monetari minimi, conservarli nell'audit e abbassare la confidenza se quantità o prezzi componenti non sono disponibili. Le righe componente devono avere `usable = false`; la sola riga padre conserva la colonna ordine.

## 6. Cipresso

Firma attesa: una riga iniziale con `COD.ART.`, `DES.ARTICOLO`, `UM`, `QT`,
`LISTINO`, `COD.EAN`, `ORDINE`. Il nome del foglio contiene normalmente la
data e non è una firma: usare il primo foglio solo dopo aver verificato le
intestazioni.

| Campo | Colonna |
| --- | --- |
| Codice articolo | A `COD.ART.` |
| Descrizione | B `DES.ARTICOLO` |
| Unità | C `UM` |
| Pezzi per cartone | D `QT` |
| Prezzo unitario netto | E `LISTINO` |
| EAN | F `COD.EAN` |
| Quantità/colli d'ordine | G `ORDINE` |

Il listino verificato non contiene colonne di IVA, sconto o disponibilità:
assumere la riga disponibile nel confronto, ma non inventare sconti. Conservare
anche EAN non standard come testo. Prima di scrivere una copia controllare
foglio, `G1 = ORDINE`, hash dell'originale e riga sorgente.

Un espositore Cipresso può apparire su una sola riga ordinabile senza righe
figlie. Riconoscerlo soltanto con regole dichiarate nel preflight (nome,
codice, quantità e prezzo coerenti); presentarlo come espositore completo e
richiedere conferma se non esistono componenti EAN+quantità con cui
riconciliarne la composizione.

## 7. Noce CSV

CSV atteso:

| Campo | Colonna CSV |
| --- | --- |
| Pagina | `catalog_page` |
| EAN | `ean` |
| Descrizione | `product` |
| Confezione | `packaging` |
| Disponibilità | `availability` |
| Variazione | `variation` |
| Prezzo unitario | `price` |
| Unità | `unit` |

Il campo `packaging` è informativo (`Um/Ct/Str`) e non deve essere convertito automaticamente in pezzi per cartone. Per i totali Noce usare soltanto il moltiplicatore esplicito in `unit`, per esempio `x 1,0`; se non è interpretabile, richiedere verifica. Una riga è utilizzabile automaticamente solo se disponibile, con prezzo valido e moltiplicatore d'ordine valido.

Accettare EAN vuoti o non standard. Non eliminare righe soltanto perché l'EAN è duplicato.

## 8. Noce listino .xls (`noce_xls_v1`)

Noce manda anche il listino completo come file Excel 97-2003, con nomi che
non dicono niente (`formattato_104233.xls`). La firma è la riga di intestazione,
mai il nome del file: `codice_a_barre`, `codice`, `descrizione_articolo`,
`pezzi_x_cartone`, `prezzo`, `quantita`, `offerta`, `Importo`, `cat`,
`ragione_sociale`. Foglio `Foglio1`, intestazione alla riga 5, primo prodotto
alla riga 6.

| Campo | Colonna |
| --- | --- |
| EAN | B `codice_a_barre` |
| Codice articolo | C `codice`, testo con gli zeri iniziali |
| Descrizione | D `descrizione_articolo` |
| Pezzi per cartone | E `pezzi_x_cartone` |
| Cartoni per strato | F, informativo |
| Strati per pallet | G, informativo |
| Prezzo **al pezzo** | H `prezzo` |
| Quantità d'ordine **in cartoni** | I `quantita`, colonna ordine |
| Offerta | J `offerta` |
| Importo | K, formula `=I*H*E` |
| Reparto | L |
| Categoria merceologica | M `cat`, vale `FOOD` o `NO FOOD` |
| Ragione sociale | N |
| Variato | O |
| Descrizione offerta | P |
| IVA | Q `Iva` |

Regole dichiarate nell'adattatore:

- **Prezzo e quantità.** La formula della colonna Importo (`=I*H*E`) dice che il
  prezzo è al pezzo e che l'unità d'ordine è il cartone. Sono i due campi che il
  resto del programma già usa (`unit_price_net` e `pieces_per_carton`): non
  serve nessuna conversione, e non va inventata.
- **Fine dei dati.** Dopo l'ultimo prodotto restano centinaia di righe che
  contengono soltanto la formula della colonna Importo (nel file misurato: dati
  fino alla riga 17148, poi 931 righe vuote fino alla 18079). Il confine è
  l'ultima riga che ha almeno uno fra EAN, codice articolo e descrizione: non va
  scritto nel codice e non va preso da `max_row`.
- **Righe FOOD.** L'utente non tratta l'alimentare: le righe con `cat = FOOD`
  non entrano nel confronto. Quante ne sono state tolte va dichiarato
  nell'audit (`inputs[].reading.rows_excluded`), mai scartato in silenzio.
- **Scadenza nella descrizione.** Circa un terzo delle descrizioni finisce con
  `<br> Scadenza gg/mm/aaaa`. La data si estrae in `expiry_date` e la
  descrizione resta pulita, altrimenti il frammento HTML sporca il confronto fra
  nomi. La data si legge ma non si crede: fuori da una finestra credibile
  (due anni indietro, dieci avanti) o impossibile sul calendario diventa
  `expiry_plausible = false` con un avviso, non un errore.
- **Offerte.** Il segnale è doppio: la colonna `offerta` e il prezzo scritto in
  grassetto («i prezzi offerta sono in grassetto», nota in D4). Basta uno dei
  due. Nel file misurato non c'è nessuna offerta attiva, ma il grassetto va
  letto lo stesso: su un listino con offerte sarebbe l'unico segnale.
- **Codici a barre.** Accettare EAN vuoti o non standard e non deduplicare per
  solo EAN: nel file misurato ci sono 55 EAN vuoti, circa 726 più corti di 13
  cifre e 46 EAN ripetuti.

## 9. Scrittura degli ordini

- Lavorare esclusivamente su copie.
- BETULLA: svuotare e riscrivere colonna C.
- Larice: svuotare e riscrivere colonna D.
- Cipresso: svuotare e riscrivere colonna G solo dopo aver verificato foglio,
  intestazione e hash dell'originale.
- Larice espositore: scrivere la quantità solo sulla riga padre; mai sulle righe componente.
- Usare la riga sorgente persistita nel risultato del matching; non rieseguire un match semantico durante la scrittura.
- Preservare formule, formattazione, fogli, filtri e righe totali.
- ⚠ **L'ordine si scrive dentro un `.xlsx`.** L'unica eccezione è Noce, che
  dichiara `order_write.mode: patch_xls_in_posizione` e a cui torna il **suo**
  `.xls` cambiato di quattro byte per cella — e quella strada pretende che la
  colonna d'ordine sia già tutta numerica a lunghezza fissa. Un `.xls` di
  chiunque altro **si legge e si confronta**, ma per compilarlo va salvato in
  `.xlsx` con Excel: non c'è nessuna conversione automatica, perché il
  documento compilato torna al fornitore e una conversione fatta in Python
  perderebbe formule, celle unite, disegni e formattazione.
