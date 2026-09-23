# Noce: si legge il loro file, si rimanda il loro file

Noce è un fornitore come gli altri: manda un documento, lo si carica, lo si
confronta, e alla fine gli si rimanda il suo stesso documento con l'ordine
dentro. Del sito non si passa più: lo scraper, le credenziali e la
preparazione dell'ordine sul sito sono stati smontati.

## Lettura

Il listino arriva come un `.xls` (Excel 97-2003, non `.xlsx`) e si carica dal
passo 1 della pagina come ogni altro listino. Lo legge `app/xls_reader.py` con
l'adattatore `noce_xls_v1` del registro (`references/adapters.json`), che
dichiara foglio, riga delle intestazioni, riga dei dati, colonna dell'EAN — per
**nome**, non per lettera — e colonna d'ordine.

Le righe FOOD si scartano in lettura: l'ordine riguarda solo il NoFood.

## Consegna

A Noce torna il **loro** `.xls`, con la sola colonna d'ordine compilata e
tutto il resto identico byte per byte. Non passa dal writer Node — che importa
ed esporta `.xlsx` e riscriverebbe il file da capo — ma da `app/xls_writer.py`,
che cambia quattro byte per cella dentro una copia.

Due guardie prima di scrivere, e costano zero perché il file va letto comunque:

1. **l'EAN della riga dev'essere quello del piano**, altrimenti si sta
   scrivendo la quantità sulla riga di un altro articolo;
2. **la colonna d'ordine dev'essere ancora tutta a lunghezza fissa (RK)**. Basta
   che una settimana ci sia una formula o una cella di tipo diverso perché la
   patch in posizione non sia più applicabile: il controllo va rifatto su ogni
   file.

Si azzera `RECALCID`: cambiare il valore di una cella non sporca le formule che
la usano, e senza questo le quantità sarebbero giuste mentre importi e totale
resterebbero a zero. Misurato.

Se la patch non si può fare, **la compilazione Noce fallisce e lo dice**:
non ripiega su un formato che il fornitore non accetta e non lascia sul disco
una copia a metà.

La prova di accettazione è il totale che il file calcola da solo, che deve
venire uguale a quello del piano. Se non torna, non si consegna.
