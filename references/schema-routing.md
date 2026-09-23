# Routing AI degli schemi e controllo errori

## Principio

Ogni run deve avere questa forma:

```text
AI preflight sugli input → motore deterministico o LLM mirato → AI check e gestione errori
```

Il determinismo non deve partire assumendo che fogli, colonne e fornitori siano identici alla run precedente.

## 1. AI preflight obbligatorio

Inventariare in sola lettura tutti i file candidati nella cartella, inclusi listini aggiuntivi. Per ogni workbook raccogliere deterministicamente nomi fogli, dimensioni attive, righe iniziali, intestazioni, tipi di dato, formule, celle unite e distribuzione dei valori nelle colonne. L'AI confronta questo profilo con gli adattatori registrati e assegna uno stato:

- `SCHEMA_NOTO`: firme e significato dei campi compatibili; usare l'adattatore deterministico;
- `SCHEMA_VARIATO`: stesso fornitore ma colonne spostate, rinominate o struttura modificata; l'AI propone una nuova mappatura;
- `NUOVO_FORNITORE`: nessun adattatore compatibile; l'AI propone identità e regole del nuovo schema;
- `FILE_NON_PERTINENTE` o `AMBIGUO`: non entra nel confronto finché non è chiarito.

La somiglianza del nome file è solo un indizio, mai una prova.

Il flusso implementato usa:

```powershell
python scripts/inspect_sources.py <input...> --output <run>\input_profiles.json
python scripts/apply_preflight_decisions.py --profiles <profili> --decisions <decisioni-ai> --output <run>\input_manifest.json
python scripts/validate_input_manifest.py --manifest <manifest> --adapters references\adapters.json --output <report>
python scripts/prepare_manifest_sources.py --manifest <manifest> --adapters references\adapters.json --output <run>\dati
```

L'inspector non compila da solo `ai_preflight`: il passaggio AI deve essere esplicito e auditabile.

## 2. Scelta del percorso

Per `SCHEMA_NOTO`, eseguire il parser deterministico registrato.

Per `SCHEMA_VARIATO`, l'AI deve mappare almeno foglio, riga iniziale, EAN, descrizione, codice, prezzo, sconto, confezione, disponibilità, IVA e colonna ordine. Se la modifica è soltanto l'ordine delle colonne e le intestazioni sono affidabili, creare una mappatura per nome intestazione. Se il significato commerciale di prezzo, sconto o quantità è ambiguo, richiedere conferma all'utente.

Se un campo non esiste davvero, dichiararlo invece di ometterlo silenziosamente: `ean_unavailable=true`, `supplier_code_unavailable=true`, `vat_unavailable=true` oppure `assume_available=true`. Per un XLSX fornitore `sheet`, `data_start_row` e `order_column` sono obbligatori; prezzo e fattore d'ordine devono essere mappati o avere un default esplicito. Il validatore rifiuta una `field_mapping` presente ma semanticamente incompleta.

Per `NUOVO_FORNITORE`, usare l'LLM per comprendere struttura e regole commerciali su un campione rappresentativo, poi salvare un adattatore dichiarativo o un parser testabile. Non processare l'intero file con interpretazione libera dell'LLM quando una mappatura stabile è stata definita.

## 3. AI check dopo il deterministico

Dopo ogni parser deterministico, l'AI deve leggere il report di audit e verificare:

- copertura dell'intero range attivo;
- conteggi righe/EAN e confronto con run precedente;
- percentuali anomale di prezzo, EAN o confezione mancanti;
- duplicati e variazioni improvvise della copertura EAN;
- campioni di righe iniziali, centrali e finali;
- prezzi netti e sconti fuori distribuzione;
- colonne ordine effettivamente individuate;
- errori o warning prodotti dal parser.

L'AI non sostituisce i conteggi con una stima: decide se il risultato è plausibile, se il parser va adattato o se serve intervento umano.

## 3-bis. Un fornitore che il registro non conosce

È la prima delle tre fermate: `SCHEMA_SCONOSCIUTO`. La pagina Importa apre
direttamente la configurazione guidata del documento coinvolto. Non si aprono
file JSON e non si lanciano comandi.

Il percorso è questo:

1. la pagina mostra il foglio e un'anteprima delle righe;
2. propone il fornitore e le colonne in base alle intestazioni, mai al nome del
   file;
3. l'utente controlla le colonne essenziali: prodotto, EAN, prezzo, pezzi per
   collo e colonna ordine; codice fornitore, IVA, unità di misura e disponibilità
   stanno in «Altre colonne», chiuso in partenza;
4. «Controlla le colonne» rilegge il file con lo stesso parser del confronto e
   mostra quante righe sono davvero utilizzabili e un campione normalizzato;
5. soltanto dopo questa prova «Conferma e ricalcola» diventa disponibile. La
   pipeline riparte da sola, costruisce il manifest e memorizza lo schema nel
   registro per le settimane successive.

Il browser non invia percorsi. Identifica la run e il profilo già prodotti
dalla pipeline; il servizio ricontrolla che la run sia ancora quella fermata,
che ogni file appartenga alla cartella dei caricamenti e che lo SHA-256 sia
quello mostrato nell'anteprima. Una colonna senza nome viene salvata per numero;
una con intestazione unica viene salvata per nome, così il registro può
riconoscerla in futuro.

Se il listino non contiene ancora una colonna ordine, la pagina permette di
scegliere la prima colonna vuota successiva ai dati. La conferma entra in
`order_write`; prima di creare la copia il programma ricontrolla impronta del
file, foglio, righe, cella d'intestazione ancora vuota e assenza di testo o
formule nella colonna. L'originale non viene modificato.

## 4. Persistenza

Per ogni run resta un `input_manifest.json` con classificazione, firma osservata,
adattatore scelto, mappatura, conferma utente e risultati del post-check. Dopo
che parsing e costruzione hanno provato davvero le colonne, l'orchestratore
esegue l'apprendimento e conserva `adattatori_imparati.json` nella cartella
della run. La decisione temporanea viene rimossa automaticamente.

Che cosa entra nel registro:

- solo le voci con `ai_preflight.state` in `SCHEMA_VARIATO` o `NUOVO_FORNITORE` **e** `user_confirmation.status == "CONFIRMED"`: l'AI propone, l'utente approva, e ciò che non è stato approvato non entra;
- solo con una `field_mapping` completa secondo `validate_input_manifest.incomplete_mapping`, cioè le stesse verifiche del validatore e non una seconda copia;
- solo se il documento, riletto con il registro appena scritto, risulta `SCHEMA_NOTO` con l'adattatore imparato: un adattatore che non riconosce nemmeno il file da cui è stato imparato è peggio di niente.

L'impronta si calcola dal profilo che sta nel manifest e non riaprendo il file: il manifest è il documento auditabile ed è quello che l'utente ha avuto davanti quando ha confermato. La voce porta `learned_at`, `learned_from` (nome e sha256 del file), `confirmed_by: "utente"` e uno `schema_version` incrementato; **la versione precedente non si perde mai**, finisce in `previous_versions`. Le regole del fornitore che non stanno nella mappatura — per esempio i `row_markers` di Larice, che dicono che `SM` non è merce acquistabile — restano dov'erano.

Ogni voce non imparata esce nel rapporto con il motivo scritto in italiano. L'uscita è `0` quando non c'è nessun errore e `2` quando una voce che doveva essere imparata è stata rifiutata: un fallimento silenzioso qui vorrebbe dire un fornitore che torna sconosciuto senza che nessuno sappia perché. `--prova` calcola e stampa tutto senza scrivere niente.

Il comparatore HTML ricava i fornitori dal manifest e dai risultati, quindi non richiede tre blocchi fissi. La scrittura di un nuovo XLSX resta invece bloccata finché la colonna ordine e le regole di copia non sono state confermate e testate.

## 5. Come si scrive l'ordine: `order_write`

Un adattatore che porta `order_write` dichiara che di quel fornitore si sa creare la copia d'ordine; uno che non ce l'ha dichiara il contrario, e **l'assenza si dice**: un fornitore nel confronto senza questa dichiarazione produce l'avviso `FORNITORE_NON_COMPILABILE`, invece di sparire in silenzio dalla configurazione di scrittura. Prima l'elenco dei compilabili era una tupla dentro `app/launcher.py`: un fornitore imparato non sarebbe mai potuto diventare compilabile senza toccare il codice, e nessuno lo diceva (misurato il 12 agosto 2026: 102 prodotti assegnati a ACERO e avvertimenti vuoti).

I campi, tutti facoltativi tranne `order_column`:

- `order_column` — la lettera della colonna in cui si scrive la quantità ordinata. Obbligatoria: senza, non si sa dove scrivere.
- `from_field_mapping` — `true` quando foglio e righe li dichiara già la `field_mapping` dell'adattatore (CIPRESSO, Noce). In quel caso la mappatura deve dichiarare **la stessa** colonna d'ordine: due colonne diverse vorrebbero dire scrivere in una cella che nessuno ha verificato, e il fornitore resta non attivato.
- `sheet`, `header_row`, `data_start_row` — per gli adattatori con un lettore dedicato, che una `field_mapping` non ce l'hanno (BETULLA, Larice). ⚠ `"sheet": "FIRST"` qui vuol dire «il primo foglio del documento», perché lo dichiara il registro, cioè una persona che quel listino l'ha guardato; `"sheet": "FIRST"` dentro una `field_mapping` vuol dire invece che il foglio **non è stato identificato**, e allora il documento ne deve avere uno solo.
- `expected_header` — il testo che deve stare nella cella d'intestazione di quella colonna. Si va a leggere davvero nel documento prima di attivare la scrittura: un listino con le colonne spostate prenderebbe l'ordine in una colonna qualsiasi.
- `required_columns` — i campi che la mappatura deve dichiarare perché la scrittura si attivi.
- `mode` — `patch_xls_in_posizione` per il `.xls` Noce, a cui si rimanda il **loro** file e non una conversione: la copia si scrive in posizione, quattro byte per cella.
