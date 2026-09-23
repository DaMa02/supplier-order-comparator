# Comparatore locale HTML

## Scelta architetturale

Usare una web app locale single-user:

```text
browser ↔ HTTP su 127.0.0.1 ↔ server Python standard library ↔ JSON della run
```

Excel e CSV restano formati di import/export, non l'interfaccia di lavoro. Non servono backend del venditore, account cloud, database esterno, npm o framework web. Lo stato della run è un JSON locale scritto atomicamente; introdurre SQLite solo se serviranno storico multi-run, utenti concorrenti o query complesse.

Questa soluzione minimizza la complessità operativa: per l'utente sono tre pagine guidate; il confine REST mantiene sostituibile il servizio locale. Streamlit è più rapido per un prototipo ma offre meno controllo sulle tabelle; NiceGUI aggiunge dipendenze e confezionamento. Per il flusso stabile attuale usare HTML/CSS/JavaScript senza strutture aggiuntive.

Le tre pagine sono:

1. importazione separata dell'elenco gestionale e dei listini fornitori;
2. prodotti da ordinare, quantità, esclusione, confronto offerte e ricerca di prodotti aggiuntivi;
3. riepilogo ordinabile per fornitore o prodotto, subtotali, soglie, promozioni e compilazione.

## Contratto

- `GET /api/review`: file, fornitori, prodotti/offerte, espositori e stato salvato;
- `POST /api/upload`: copia locale base64, profilo read-only e risposta `PROFILED_AI_PENDING`;
- `GET /api/schemas/pending`: per una run fermata su `SCHEMA_SCONOSCIUTO`,
  anteprima dei soli documenti coinvolti, fornitori esistenti e proposta delle
  colonne ricavata dalle intestazioni;
- `POST /api/schemas/validate`: prova le colonne scelte con il parser reale,
  senza salvarle né rilanciare; restituisce conteggi e campione normalizzato;
- `POST /api/schemas/confirm`: ripete la prova, salva la conferma in modo
  atomico e avvia il ricalcolo; risponde `202` con il nuovo stato pipeline;
- `PUT /api/state`: autosalvataggio di quantità, esclusioni, fornitore, conferme, pagina, raggruppamento e consenso soglia; restituisce anche lo stato aggiornato delle promozioni;
- `GET /api/products/search?q=<testo>`: ricerca conservativa nei cataloghi correnti;
- `POST /api/products/add`: aggiunta locale di un prodotto scelto dai cataloghi;
- `POST /api/compile`: validazione finale e generazione del piano/copie configurate;
- `GET /api/history/pending`: ordini compilati e non ancora dichiarati ricevuti;
- `POST /api/history/answer`: risposta unica per l'intero ordine; `received`
  sì chiude come ricevuto, `received` no rimanda la domanda di sette giorni,
  `closed: true` chiude come «non arriverà più» senza dichiararlo ricevuto;
- `POST /api/matches/answer`: accetta o rifiuta la riga di listino proposta per
  un prodotto; la decisione è vincolata a run, fornitore e impronta della riga;
- `POST /api/uploads/elimina`: elimina una singola copia caricata e il suo
  profilo, soltanto se il nome compare fra gli upload eliminabili del server;
- `GET /api/ordini`: le compilazioni già fatte, dalla più recente, ricavate
  scandendo le cartelle datate;
- `POST /api/ordini/elimina`: elimina una compilazione elencata dal server e,
  nella stessa operazione, tutti i promemoria che hanno la sua chiave stabile;
- `GET /ordini/<cartella>/<nome>`: scarica un documento di quella compilazione;
- `GET /ordini/<cartella>/zip`: i soli listini di quella compilazione in un
  archivio costruito al momento in memoria;
- `GET /outputs/<nome>`: rotta residua. Dallo smontaggio del carrello Noce
  il programma non scrive più artefatti di servizio: serve solo a scaricare
  quelli rimasti dalle run passate. Le consegne al fornitore non passano di
  qui: stanno nella cartella datata della compilazione.

## Storico degli ordini non ancora ricevuti

Ogni compilazione riuscita registra una voce per fornitore in
`app/data/history/orders.json`, **fuori dalla cartella della run** per
sopravvivere al ricalcolo settimanale. Il percorso si può indicare con
l'opzione `--history`; in sua assenza si ricava da
`<cartella dello stato>/../history/orders.json`.

Regole:

- la chiave delle voci nuove è `<cartella-compilazione>:<fornitore>`: due
  compilazioni sono due documenti distinti; una riconsegna dello stesso
  fornitore sostituisce la domanda ancora aperta della stessa run, invece di
  duplicarla;
- entra nello storico soltanto il fornitore di cui il writer ha lasciato una
  copia fedele sul disco. Una compilazione senza copie non è un ordine. Un
  fornitore omesso da una compilazione successiva non cancella però il listino
  già prodotto prima, che potrebbe essere già stato inviato;
- la run attualmente aperta non compare mai fra gli ordini in attesa, altrimenti
  chiederebbe conto di merce ordinata pochi istanti prima;
- l'abbinamento usa l'EAN; senza EAN ammette soltanto un identificativo stabile
  costruito dalla composizione dell'espositore. Gli identificativi
  `product:<riga>` e `display:unmatched:` non sono identità fra settimane;
- quando più prodotti condividono l'EAN, l'avviso dichiara l'ambiguità e non
  attribuisce la quantità ordinata a ciascuno come se fosse tutta sua;
- una voce oltre i 60 giorni dall'ultima interazione, o con data illeggibile,
  scade. La scadenza viene annunciata in pagina per 30 giorni, non sparisce in
  silenzio;
- se la registrazione fallisce la compilazione resta valida, ma l'avviso deve
  comparire nel messaggio finale: senza promemoria la funzione non serve a nulla.

Le consegne parziali non esistono. Le tre risposte riguardano l'intero ordine:
«ricevuta», «non ancora» e «non arriverà più».

Snapshot minimo:

```json
{
  "runId": "run-id",
  "currentStep": 3,
  "acceptBelowThreshold": false,
  "summaryGrouping": "supplier",
  "products": [
    {"id": "product:12", "quantity": 10, "selectedSupplierId": "larice", "confirmed": true, "excluded": false}
  ]
}
```

Il backend ricalcola prezzi e totali dai dati della run: non fidarsi di prezzi o totali inviati dal browser.

## Decisioni rapide nell'interfaccia

Nella pagina di confronto il nome del prodotto del gestionale resta distinto
dalla descrizione della riga proposta dal fornitore. Una proposta scartata ma
plausibile presenta subito soltanto la domanda `È lo stesso prodotto?` con
`Sì` e `No`; punteggio, metodo e motivazione sono nel dettaglio chiuso
`Perché compare?`. Un `Sì` rende l'offerta utilizzabile per il confronto
corrente, un `No` la nasconde. La decisione non sopravvive a una riga di
listino cambiata.

Durante lo scorrimento una fascia sottile mantiene visibili i totali correnti
per fornitore. Le schede espongono prezzo per collo, prezzo per pezzo, pezzi
per collo e totale, senza ripetere l'equazione dei colli. Nel riepilogo l'EAN è
quello dell'offerta selezionata e i comandi `−`, `+`, `×` modificano i colli o
portano il prodotto a zero.

Tutte queste modifiche seguono l'autosalvataggio esistente: lo snapshot viene
scritto atomicamente circa **450 ms** dopo la modifica. Quando la pagina mostra
`Tutto salvato`, quantità, fornitore, conferme, esclusioni e pagina tornano dopo
la riapertura. La temporizzazione non va aumentata o ridotta.

Compilazioni precedenti e dettagli secondari sono chiusi in partenza. Le
cancellazioni richiedono conferma sulla singola riga. Rimuovere un listino
caricato non modifica il confronto vivo: questo cambia soltanto dopo un nuovo
ricalcolo concluso con successo.

`quantity` indica sempre il numero di colli: nessun arrotondamento, nessuna eccedenza. Il backend conserva i pezzi consegnati (`colli × pezzi per collo`) come dato informativo. Per un espositore indica il numero di espositori completi. Lo snapshot trasporta anche `quantitySource` (`gestionale` o `utente`), che distingue la quantità precompilata dalla colonna `Colli` da quella scelta dall'utente.

## Avvio

Per il dataset corrente usare `AVVIA_COMPARATORE.cmd`. Il launcher cerca prima il runtime Python bundled di Codex e poi `py`/`python`, apre il browser e mantiene dati sotto `app/data/`.

Quando trova anche Node 18+, crea `app/data/current/writer_config.json` dai `sourcePath` verificati dei file BETULLA/Larice e abilita le copie XLSX. Se manca un requisito, il launcher lo dichiara e lascia disponibile il piano JSON; non crea configurazioni parziali.

Le copie `.xlsx` le scrive `scripts/lib/xlsx_in_posizione.mjs` senza nessuna libreria: apre lo ZIP del listino con `node:zlib`, cambia le sole celle della colonna d'ordine dentro l'XML del foglio, e richiude copiando ogni altra parte byte per byte. Fino al 5 settembre 2026 lo faceva `@oai/artifact-tool`, una libreria .NET in WebAssembly che importava il documento e lo riscriveva da capo: è il motivo per cui esiste il confronto cella per cella qui sotto, che resta.

Ogni copia prodotta viene riaperta e confrontata **cella per cella** con il suo listino di partenza (`app/copia_fedele.py`) prima di essere consegnata: le uniche differenze ammesse sono nella colonna d'ordine (le quantità del piano e l'azzeramento di quelle preesistenti). Il confronto carica i libri **per intero** (mai `read_only`: una `<dimension>` prudente o le righe fuori ordine sono lecite e non devono far rifiutare una copia perfetta) e guarda anche il **formato numerico** dove la copia mostra un valore — un prezzo con formato `0` o una quantità nascosta da `;;;` cambiano quello che il fornitore legge senza cambiare la memoria del file. Una copia che non regge il confronto viene cancellata e il motivo finisce fra gli avvisi della compilazione. Dichiarato e non guardato: stili e colori, larghezze, immagini, filtri automatici, proprietà del documento, risultati in cache delle formule (le formule si confrontano come testo).

Per una run diversa avviare direttamente `app/server.py` con `--review`, `--state`, `--uploads` e `--output-dir`. L'host deve restare loopback. Non esporre la porta sulla LAN.

La query `?demo=1` è soltanto una demo in memoria. In modalità normale un backend assente deve produrre un errore esplicito; non usare fallback dimostrativi nascosti.

## Upload e ciclo AI

Il caricamento non autorizza un parsing automatico cieco. Dopo l'upload:

1. il server crea una copia univoca e calcola il profilo;
2. Codex esegue il preflight AI;
3. manifest e adattatore vengono validati;
4. parser deterministico o LLM mirato elaborano il file;
5. Codex controlla l'audit e rigenera `review_data.json`;
6. la pagina viene aggiornata.

Finché questo ciclo non termina mostrare `AI pending`; non mescolare il nuovo listino con dati della run precedente.

## Vincoli di compilazione

La compilazione è bloccata quando l'ordine è vuoto, una quantità non è intera, l'offerta è indisponibile, manca una conferma o un totale sotto soglia non è stato esplicitamente accettato. Modificare quantità o fornitore azzera il consenso sotto soglia.

Gli espositori mostrano prezzo per espositore, prezzo per pezzo, quantità dichiarata, confidenza e composizione espandibile. Le righe componente non sono selezionabili come prodotti autonomi.

Gli sconti numerici certi sono già riflessi nel prezzo importato o sono calcolati in un campo separato. Omaggi, campioncini, confezioni promozionali e condizioni ambigue sono visibili nelle offerte e nel riepilogo, ma non modificano automaticamente i totali né il fornitore scelto.

## Sicurezza e output

- ascoltare solo su `127.0.0.1`, `localhost` o `::1`;
- accettare soltanto `.xlsx`, `.xls` e `.csv` entro i limiti configurati (il
  `.xls` è il formato con cui arriva il listino Noce);
- sanificare nomi e percorsi, non sovrascrivere upload o originali;
- applicare CSP e header anti-framing;
- scrivere lo stato atomicamente;
- creare sempre `final_order_plan.json` prima delle copie XLSX, **dentro la
  cartella datata di quella compilazione** (`<dati>/ordini/2026-08-12_1435/`):
  una compilazione non sovrascrive mai i listini pronti della precedente, e
  l'elenco delle compilazioni si ricava scandendo quelle cartelle, non da un
  indice a parte che potrebbe disallinearsi;
- chiudere ogni percorso di scaricamento con una lista bianca: il nome chiesto
  deve comparire identico in una scansione della cartella, e il percorso
  risolto deve restare dentro la radice degli ordini;
- mostrare e conservare soltanto copie/piani relativi a fornitori con almeno una riga ordinata;
- rimuovere i sidecar diagnostici del motore fogli dalla cartella consegnata;
- non inviare ordini.
