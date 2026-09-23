---
name: supplier-order-comparator
description: Confronta un export gestionale con listini fornitori Excel/CSV, inclusi BETULLA, Larice, Noce e nuovi schemi, riconosce espositori composti, propone match EAN/semantici controllati e guida quantità, scelta fornitore e compilazione in una web app locale. Usare per il workflow periodico AI-preflight → parser deterministico o LLM mirato → AI post-check, senza modificare gli originali né inviare ordini.
---

# Compara ordini fornitori

## Principi non negoziabili

Trattare ogni input come read-only. Separare sempre analisi/revisione da compilazione. Non inviare mai un ordine senza una successiva conferma esplicita.

Ogni run segue:

```text
AI preflight input → adattatore deterministico o LLM mirato → AI post-check → revisione utente → copie d'ordine
```

Usare il determinismo per scansione, identificatori, calcoli, riconciliazioni, archiviazione e scrittura. Usare l'AI per comprendere schemi variati, equivalenza commerciale, eccezioni e interfacce variabili. `DA_VERIFICARE` è un risultato valido.

## Riferimenti obbligatori

Leggere prima di agire:

- `references/source-schemas.md` per campi, prezzi, espositori e colonne d'ordine;
- `references/schema-routing.md` per il preflight e il manifest;
- `references/matching-policy.md` per EAN, composizioni e matching semantico;
- `references/ai-decision-format.md` prima di salvare decisioni AI;
- `references/web-app.md` prima di avviare o modificare il comparatore locale;
- `references/decision-boundaries.md` nei casi ibridi;
- `references/noce.md` prima di leggere o compilare il documento Noce.

## La catena si lancia da un pulsante

Dalla Fase 6c i passi qui sotto **non si lanciano più a mano**: li esegue
`app/pipeline_jobs.py` quando l'utente preme «Ricalcola il confronto» nella
pagina 1 (`POST /api/pipeline/avvia`, avanzamento su `GET /api/pipeline/stato`).
Ogni esecuzione nasce in una cartella datata sotto `<dati>/esecuzioni/`, e il
confronto vivo viene sostituito **solo alla fine**: se una fase fallisce,
`review_data.json` resta quello di prima.

I comandi restano documentati perché sono l'interfaccia vera dei passi — è così
che l'orchestratore li esegue, ed è così che si prova un passo da solo. Quello
che cambia è chi li chiama.

```text
PROFILAZIONE → RICONOSCIMENTO → VALIDAZIONE → PARSING → SHORTLIST
→ VALUTAZIONE_AI → RISOLUZIONE → COSTRUZIONE → ATTIVAZIONE
```

Le fermate sono **tre**, ed è dove il programma non ha l'autorità per
decidere perché l'ingresso non si può usare: uno schema che il registro non
conosce — si rimedia in tre passi nell'ordine (decisione in
`<dati>/decisioni_schemi.json`, ricalcolo, `scripts/impara_adattatore.py` sul
manifest che il ricalcolo ha prodotto: il formato e l'esempio stanno in
`references/schema-routing.md`, sezione «Un fornitore che il registro non
conosce») — un documento che non si legge affatto, e un listino da cui non
esce nemmeno una riga ordinabile (`FORNITORE_SENZA_RIGHE`: un fornitore
caricato apposta non può sparire dal confronto con un semplice avviso; si
sostituisce o si toglie il documento e si rilancia, oppure — se il fornitore
ha cambiato la forma del listino — si passa dalla fermata degli schemi).
A queste si aggiungono le precondizioni dell'attivazione, che sono numeri:
prodotti > 0, fornitori > 0, i casi ricevuti dalla fase AI uguali ai candidati
prodotti, nessuna coppia semantica senza shortlist. **Tutto il resto avvisa e
non ferma**, e l'avviso finisce in cima alla pagina.

Il riconoscimento genera da sé le decisioni che `apply_preflight_decisions.py`
pretendeva scritte a mano, prendendole dal registro insieme alla loro
`field_mapping`. Di due listini dello stesso fornitore usa il **più recente** e
dice quale è rimasto fuori.

## Workflow

### 1. Profilare tutti gli input

Ogni esecuzione nasce già in una cartella datata, quindi non c'è niente da archiviare a mano. Inventariare tutti gli XLSX/CSV candidati, inclusi file con nomi nuovi:

```powershell
python scripts/inspect_sources.py <cartella-o-file...> `
  --recursive `
  --output <run>\input_profiles.json
```

L'inspector legge nomi foglio, range attivi, intestazioni, tipi, formule, celle unite e campioni iniziali/centrali/finali. Il nome del file è soltanto un indizio.

L'AI esamina i profili e produce decisioni esplicite con uno dei seguenti stati: `SCHEMA_NOTO`, `SCHEMA_VARIATO`, `NUOVO_FORNITORE`, `FILE_NON_PERTINENTE`, `AMBIGUO`. Per gli ultimi due non procedere. Per uno schema variato o nuovo, indicare una `field_mapping` completa e richiedere conferma se prezzo, sconto, unità d'ordine o colonna di scrittura sono ambigui.

```powershell
python scripts/apply_preflight_decisions.py `
  --profiles <run>\input_profiles.json `
  --decisions <run>\preflight_decisions.json `
  --output <run>\input_manifest.json

python scripts/validate_input_manifest.py `
  --manifest <run>\input_manifest.json `
  --adapters references\adapters.json `
  --output <run>\manifest_validation.json
```

Richiedere esattamente un master, `supplier_id` univoci e hash invariati. Non avviare il parser se la validazione fallisce.

### 2. Normalizzare e riconoscere gli espositori

```powershell
python scripts/prepare_manifest_sources.py `
  --manifest <run>\input_manifest.json `
  --adapters references\adapters.json `
  --output <run>\dati
```

Gli schemi noti riusano parser testati; quelli variati o nuovi usano la mappatura dichiarativa confermata. Il parser conserva gli EAN come testo e non usa `Colli`/`Quantità` storici del gestionale come quantità d'ordine.

Per Larice viene eseguito anche il rilevatore di espositori. Un espositore è una riga padre ordinabile seguita da righe componente non ordinabili. Valutare insieme:

- nome/etichetta (`ESPO`, `EXPO`, `DISPLAY`, `CASSA MISTA`) e assenza di negazioni come `NO ESPO`;
- presenza di codice, prezzo e colonna ordine sulla riga padre;
- assenza del codice articolo fornitore sulle righe figlie; un EAN componente può invece essere presente;
- continuità del blocco e descrizioni dei componenti;
- riconciliazione tra quantità dichiarata e somma componenti;
- riconciliazione tra prezzo padre e somma `quantità × prezzo componente`.

Le righe figlie sono sempre escluse dagli articoli ordinabili. Un'offerta con evidenze incomplete resta a confidenza media e richiede conferma. Per una diagnosi isolata usare `python scripts/detect_displays.py <larice.xlsx> --summary`.

Eseguire l'AI post-check su `audit.json`: copertura del range attivo, conteggi, EAN, duplicati, prezzi, sconti, confezioni, righe ordine, campioni e warning. Un crollo anomalo richiede un'indagine, non una correzione AI dei conteggi.

### 3. Risolvere i match

Accettare automaticamente soltanto un EAN esatto, unico e utilizzabile. Per assenti o duplicati:

```powershell
python scripts/build_semantic_shortlists.py `
  --normalized <run>\dati\normalized_sources.json `
  --queue <run>\dati\semantic_queue.json `
  --output <run>\dati\semantic_shortlists.json `
  --top-k 5
```

L'AI valuta solo i candidati prodotti, confrontando marca, famiglia, formato, multipack, variante e forma. Il passo è una CLI come le altre:

```powershell
python scripts/valuta_shortlist.py `
  --shortlists <run>\dati\semantic_shortlists.json `
  --output <run>\dati\ai_decisions.json `
  --rapporto <run>\dati\ai_rapporto.json
```

Scrive **sempre tutti e due i file**, anche quando la fase è degradata. Gli stati senza decisione si omettono dal file invece di diventare un `UNRESOLVED` finto: `merge_match_decisions.py` tratta già una coppia senza decisione come `DA_VERIFICARE`, e inventarla farebbe sparire la differenza fra «il modello non ha saputo» e «non ho potuto chiedere» — che è quella che si rimedia rilanciando.

| Esito | Significato |
|---|---|
| `0` | valutato tutto quello che c'era da valutare |
| `2` | errore d'uso: shortlist illeggibile o malformata, uscite non scrivibili |
| `5` | degradato: i due file ci sono, ma una parte dei casi non è stata valutata (rete, tetto di spesa, chiave assente). **La catena prosegue**: «se OpenRouter non risponde il programma tira dritto» è una decisione presa |

Il rapporto porta casi ricevuti, valutabili, decisi, per stato, per azione, chiamate, spesa, durata, modello e versione del prompt — ed è da lì che si prende `--decisions-attese`. I casi **senza nessun candidato** non si mandano al modello (sei su 948 nella run vera) e si contano a parte: chiedergli di scegliere fra niente è un uso sbagliato dell'API.

⚠ **Se nessuno dei casi ricevuti ha candidati, l'esito è `5` e non `0`.** Una coda vuota (`[]`) vuol dire che l'EAN ha risolto tutto ed è un successo; centinaia di voci con `candidates: []` vogliono dire che il passo dei candidati non ha prodotto niente — di solito perché un listino non si è caricato — e dichiararlo «valutato tutto» sarebbe un fallimento che somiglia a un successo.

Poi:

```powershell
python scripts/merge_match_decisions.py `
  --matching <run>\dati\matching_result.json `
  --normalized <run>\dati\normalized_sources.json `
  --shortlists <run>\dati\semantic_shortlists.json `
  --decisions <run>\dati\ai_decisions.json `
  --decisions-attese <quante ne ha prodotte la fase AI> `
  --output <run>\dati\resolved_matches.json
```

`--decisions-attese` è **obbligatorio**: senza, un file delle decisioni mancante è indistinguibile da «l'AI non ha deciso niente» e la catena finisce con esito 0 e un confronto in cui nessun caso semantico è stato valutato. Chi chiama dichiara il numero, e il numero deve tornare. Zero è legittimo e vuol dire «la fase AI è degradata e lo sa».

⚠ **Il numero si prende da `ai_rapporto.json`, non contando `ai_decisions.json`.** Contare il file che si sta per riconciliare rende il confronto una tautologia e riapre la porta che stava chiudendo: la fase AI dichiara quanti casi ha deciso a partire dalla propria contabilità, e quel numero è indipendente dal file.

| Esito | Significato | File scritto |
|---|---|---|
| `0` | tutto risolto | sì |
| `2` | ingresso non utilizzabile: file illeggibile, decisioni che non sono una lista, azione non ammessa, `ACCEPT` senza una riga utilizzabile | no |
| `3` | decisioni non riconciliate: il numero non torna, ce ne sono di duplicate, il file indicato non esiste, alcune non si legano a nessuna coppia di questa run, oppure sono state prese su un caso diverso da quello di oggi | negli ultimi due casi |
| `4` | il listino non è più quello su cui il modello ha deciso: le coppie interessate sono degradate a `DA_VERIFICARE` | **sì** |
| `5` | il modello ha indicato una riga che non gli è stata mostrata, o — su un `EAN_AMBIGUO` — una riga che non ha l'EAN del prodotto: stessa degradazione | **sì** |

Negli esiti `4` e `5` il file si scrive lo stesso, con le coppie guaste già degradate. Non scriverlo sembrava più prudente e non lo era: resterebbe sul disco il `resolved_matches.json` della run precedente, e `build_review_data.py` lo leggerebbe come se fosse di oggi. Ogni coppia degradata porta `ai_decisione_scartata`, che `build_review_data.py` conta in un avviso in cima alla pagina: senza, in elenco sarebbe indistinguibile da un prodotto che l'AI non ha mai valutato.

Una decisione la cui coppia nel frattempo l'EAN ha risolto da solo **non** è un guasto: si conta a parte (`decisioni_superate_dall_ean`) e non ferma niente. Succede ogni volta che si rifanno i passi deterministici dopo la fase AI, e il risultato è migliore di quello che l'AI proponeva.

La riga accettata si verifica contro **la shortlist**, che è l'unica cosa che il modello ha visto, e non contro `matching_result.json` — che `prepare_sources.py` scrive insieme al listino normalizzato, e quindi non può disallinearsi da esso. L'identità è EAN + descrizione + prezzo, e il prezzo si confronta **anche quando arriva come stringa**, perché `prepare_sources.py` lo serializza così.

⚠ Ogni decisione porta `ai_impronta_caso`, ed è **obbligatoria**. Tutte le guardie qui sopra confrontano fra loro artefatti della run corrente, quindi rispetto a un file di decisioni di un'altra run sono cieche per costruzione: il gestionale è lo stesso file di settimana in settimana, le coppie si sovrappongono quasi tutte e i conteggi riconciliano. L'impronta è calcolata su ciò che il modello ha visto davvero — articolo cercato e l'elenco ordinato dei candidati — e `merge_match_decisions.py` la ricalcola dalla shortlist di oggi. Chi non corrisponde non entra: `DECISIONE_DI_UNA_ALTRA_RUN` se l'impronta è diversa, `DECISIONE_SENZA_IMPRONTA` se non c'è affatto, esito `3` in tutti e due i casi.

L'impronta prova che il caso è **identico**, non che la decisione sia di oggi: se il listino di un fornitore non cambia, il caso è lo stesso e la decisione vecchia passa — che è quello che fa apposta la memoria delle risposte. Per questo ogni decisione porta anche `ai_modello`, `ai_versione_prompt` e `ai_versione_avversario`: il merge le legge e non le giudica, perché la configurazione viva non ce l'ha. **Confrontarle è lavoro dell'orchestratore.**

⚠ **Ogni coppia semantica deve avere la sua shortlist.** Se `matching_result.json` dichiara 948 coppie non `EAN_ESATTO` e `semantic_shortlists.json` ne porta 500, la fase AI ne valuta 500, ne dichiara 500, qui se ne trovano 500 e tutto riconcilia — perché le due parti contano la stessa cosa mancante. Quattrocentoquarantotto prodotti sparirebbero dalla valutazione con esito 0 dappertutto. Ora si contano (`coppie_senza_shortlist`) e l'esito è `3`: vanno rifatti i candidati e la fase AI, non riletto il listino.

Un match AI resta visibile e richiede conferma utente. Dopo le decisioni, il merge porta il codice a barre di una riga accettata dall'AI presso un fornitore agli altri fornitori che non hanno abbinamento (`EAN_DA_ALTRO_FORNITORE`, sempre da confermare; regole in `references/matching-policy.md` §6): il riepilogo lo conta in `abbinamenti_per_stesso_codice`. Per confrontare espositori tra fornitori usare anzitutto il fingerprint della composizione `EAN + quantità`; un nome simile da solo non dimostra composizione equivalente.

### 4. Generare il comparatore locale

L'interfaccia principale è HTML, non Excel:

```powershell
python scripts/build_review_data.py `
  --resolved <run>\dati\resolved_matches.json `
  --manifest <run>\input_manifest.json `
  --audit <run>\dati\audit.json `
  --displays <run>\dati\display_offers.json `
  --threshold 1000 `
  --run-id <run-id> `
  --output <run>\review_data.json

python app/server.py `
  --review <run>\review_data.json `
  --state <run>\web_state.json `
  --uploads <run>\uploads `
  --output-dir <run>\ordini `
  --history <cartella-dati>\history\orders.json
```

`--history` è facoltativo: senza di esso lo storico degli ordini non ancora
ricevuti finisce in `<cartella dello stato>\..\history\orders.json`. Deve restare
fuori dalla cartella della run, altrimenti il ricalcolo settimanale cancellerebbe
la memoria delle consegne in sospeso.

Aprire `http://127.0.0.1:8765`. Per il dataset corrente usare il launcher Windows fornito; vedere `references/web-app.md`.

La UI deve mantenere tre pagine semplici:

1. `Importa i dati`: separare l'elenco prodotti del gestionale dai listini fornitori;
2. `Scegli prodotti e fornitori`: per ogni articolo mostrare quantità desiderata, esclusione, confronto completo delle offerte e composizione espandibile degli espositori; consentire anche la ricerca e aggiunta di prodotti non presenti nel gestionale;
3. `Riepilogo e compilazione`: raggruppamento per fornitore o prodotto, ordinamenti, subtotali, soglie, promozioni e comando finale di compilazione.

La quantità richiesta dall'utente è sempre in colli: non esiste arrotondamento né eccedenza. La colonna `Colli` del gestionale precompila il campo (`quantitySource: "gestionale"`); appena l'utente la modifica il prodotto passa a `quantitySource: "utente"`. Per espositori e kit la quantità indica unità complete. Il totale di riga è `colli × prezzo per collo`, ma il confronto fra fornitori si fa sul prezzo al pezzo, perché colli di fornitori diversi contengono quantità diverse.

Un upload nuovo viene solo copiato e profilato: entra nel confronto quando l'utente preme «Ricalcola il confronto», che avvia la catena della 6c. Non presentare dati vecchi come se includessero il nuovo file.

### 5. Validare e compilare

Prima di `COMPILA LISTINI` richiedere:

- almeno una quantità intera positiva;
- un'offerta disponibile per ogni prodotto ordinato;
- conferma dei match semantici e degli espositori a confidenza non alta;
- consenso esplicito per ogni totale `0 < totale < EUR 1.000`.

Il backend crea `final_order_plan.json` e salva lo stato localmente. Ogni compilazione ha la **sua cartella datata** (`<dati>/ordini/2026-08-12_1435/`) e non sovrascrive mai i listini pronti della volta prima: dentro finiscono il piano, le copie dei listini con nomi leggibili (`Ordine LARICE — 12 agosto 2026.xlsx`) e `compilazione.json`, l'audit che descrive la cartella. La pagina elenca le compilazioni precedenti e consegna i listini con un pulsante «Scarica», cioè uno zip datato costruito al momento. Se configurato con `--writer-config`, chiama il writer deterministico e crea copie dei listini; altrimenti consegna il piano validato per la fase successiva. Sconti numerici certi possono concorrere ai prezzi; omaggi, campioncini e offerte ambigue restano informativi e non alterano automaticamente totale o scelta del fornitore.

Per gli schemi correnti:

```powershell
node scripts/write_supplier_orders.mjs `
  --plan <run>\ordini\final_order_plan.json `
  --betulla <betulla-originale.xlsx> `
  --larice <larice-originale.xlsx> `
  --output-dir <run>\ordini
```

BETULLA scrive in C; Larice in D. Per un espositore scrivere solo sulla riga padre. Il writer importa gli originali e salva nuove copie. Verificare visivamente le copie e riconciliare celle, quantità e totali con il piano. Per un nuovo fornitore la comparazione è dinamica, ma la scrittura XLSX richiede prima una colonna ordine confermata e un writer/adattatore testato.

Il writer non ha nessuna libreria: `scripts/lib/xlsx_in_posizione.mjs` apre lo ZIP del listino con la sola libreria standard di Node, cambia le celle della colonna d'ordine dentro l'XML del foglio e richiude copiando ogni altra parte byte per byte (dal 5 settembre 2026; prima una libreria .NET in WebAssembly ricostruiva il file da capo). Nella colonna d'ordine il writer azzera **solo le quantità** — un numero — e lascia stare le righe che prodotto non sono, come i titoli di sezione del listino Larice. Ogni copia viene poi confrontata cella per cella con l'originale (`app/copia_fedele.py`) — valori e formati numerici dove la copia mostra un valore, libri caricati per intero — e, se differisce fuori dalla colonna d'ordine, non viene consegnata. Gli errori del writer arrivano in pagina come una frase in italiano marcata `ERRORE_COMPILAZIONE:`; la marca va solo sulle frasi del writer, il dettaglio tecnico resta sulla console.

Consegnare soltanto le copie dei fornitori che hanno almeno una riga ordinata; rimuovere le copie vuote.

### 6. Noce: si rimanda il loro documento

**A Noce torna il loro `.xls`**, con la sola colonna d'ordine compilata e
tutto il resto identico byte per byte. Deciso da Daniele il 12 agosto 2026 e
fatto nella 6e: non passa dal writer Node — che importa ed esporta `.xlsx` e
riscriverebbe il file da capo — ma da `app/xls_writer.py`, che cambia **quattro
byte per cella** dentro una copia.

Regole, tutte non negoziabili:

- **La regola sta nel registro.** Foglio, riga dei dati, riga delle
  intestazioni, colonna d'ordine e nome della colonna dell'EAN vengono dalla
  `field_mapping` di `noce_xls_v1`. La colonna dell'EAN resta dichiarata per
  **nome** e si risolve leggendo l'intestazione del file: `cat` e `Iva` sono
  nomi di colonna veri di questo listino e insieme riferimenti Excel validi, e
  un ripiego sulle lettere leggerebbe in silenzio la colonna sbagliata.
- **Due guardie prima di scrivere**, e qui costano zero perché il file va letto
  comunque: l'EAN della riga dev'essere quello del piano, e la colonna d'ordine
  dev'essere ancora **tutta a lunghezza fissa** (RK). Il secondo controllo va
  rifatto a ogni file: basta che una settimana ci sia una formula o una cella
  vuota perché la patch non sia più applicabile.
- **Se la patch non si può fare, la compilazione Noce fallisce e lo dice.**
  Non ripiega su un formato che il fornitore non accetta, e non lascia sul disco
  una copia a metà. Il foglio a parte con le sole righe ordinate resta
  un'uscita d'emergenza per l'utente, non un formato di consegna.
- ⚠ **Si azzera `RECALCID`.** Cambiare il valore di una cella **non sporca** le
  formule che la usano: senza questo, le quantità sarebbero giuste e gli importi
  e il totale resterebbero a zero. Misurato.
- **La prova di accettazione è il totale che il file calcola da solo**, che deve
  venire uguale a quello del piano. Se non torna, non si consegna.

## Regole di arresto

Fermarsi se un file è ambiguo; il manifest non è valido; Noce o il post-check falliscono; una decisione AI esce dalla shortlist; composizione/prezzo di un espositore non è riconciliabile; manca una conferma richiesta; un ordine sotto soglia non è stato accettato; oppure l'azione successiva invierebbe definitivamente un ordine.
