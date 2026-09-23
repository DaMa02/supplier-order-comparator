# Promozioni: contratto di integrazione

## Scopo

Il motore in `scripts/promotions.py` trasforma le annotazioni dei listini in
regole strutturate. Non modifica i file originali, non cambia il fornitore
scelto e non sottrae il valore degli omaggi dal totale.

Gestisce quattro casi:

1. `sconto_numerico`: percentuale esplicita e calcolabile;
2. `soglia_omaggio`: acquisto di una quantità minima con premio;
3. `confezione_promozionale`: contenuto aggiuntivo già dentro la confezione;
4. `offerta_ambigua`: testo promozionale che richiede conferma.

## Contratto di una promozione

```json
{
  "id": "promo:noce:...",
  "supplier": "noce",
  "source_reference": "csv:1453-1460",
  "source_text": "ACQUISTA 5 CT IN OMAGGIO 1 CT (12 PEZZI)...",
  "kind": "soglia_omaggio",
  "threshold": {"qty": 5, "unit": "cartoni"},
  "eligible": {
    "products": ["product:1", "product:2"],
    "eans": [],
    "source_rows": [1453, 1454],
    "group": "chiary-intimo",
    "mix_allowed": true
  },
  "reward": {
    "kind": "prodotto",
    "description": "CHIARY INTIMO GEL MINI ML.50",
    "qty": 1,
    "unit": "cartoni",
    "pieces_per_unit": 12,
    "ean": null,
    "supplier_code": null
  },
  "repeatable": true,
  "certainty": "alta",
  "confirmed": true,
  "economic_effect": {
    "type": "informational_reward",
    "discount_rate": null,
    "deterministic": true,
    "active": true,
    "affects_total": false,
    "affects_supplier_choice": false,
    "base_price_field": "unitPricePreDiscount",
    "already_applied": false
  }
}
```

`eligible` deve avere almeno un riferimento concreto: identificativi prodotto,
EAN, righe del listino oppure un gruppo presente nei prodotti o nelle offerte.
Senza questo collegamento la regola resta `da_verificare` e non viene calcolata.

## Dove un fornitore scrive le sue condizioni: lo dice il registro

⚠ **Fino al 15 agosto 2026 il lettore conosceva un fornitore solo.** Andava a
prendere `larice_v1` per nome, apriva il documento di `paths["larice"]` e
scriveva «LARICE» nei messaggi: le condizioni degli altri non le guardava
nessuno, e non c'era modo di dichiararle senza rimettere mano al codice. Adesso
la domanda che il ponte fa al registro è «quali fornitori dichiarano dove
tengono le loro condizioni?», e la risposta sta in `references/adapters.json`.

Un adattatore che vuole essere letto aggiunge una voce `commercial_conditions`:

```json
"commercial_conditions": {
  "note": "perché sta qui e su quale listino è stato misurato",
  "layout": "blocchi",
  "sheet": "FIRST",
  "data_start_row": 1,
  "max_block_rows": 500,
  "fields": {
    "text": "description",
    "reward": "reward_description",
    "ean": "ean",
    "row_code": "discount"
  }
}
```

- **`layout`** — la forma in cui il fornitore scrive. Due valori:
  - `blocchi`: una condizione occupa più righe — l'intestazione con la
    quantità da acquistare, poi la merce ammessa, poi la riga con l'omaggio.
    È come scrive LARICE, ed è l'unica forma misurata su un listino vero.
    Pretende tutte e quattro le colonne: senza il nome del premio la soglia si
    ricompone lo stesso ma esce `da_verificare`, e l'utente perde l'omaggio in
    un altro modo. ⚠ `reward` **può nominare la stessa colonna di `text`**,
    quando il fornitore scrive il nome del premio dentro la riga stessa: è come
    scrive il canvass nuovo di LARICE («IN OMAGGIO 1CT SH. A/ERBAR. 250ML
    LAVANDA», tutto in colonna E). In quel caso la colonna si legge una volta
    sola — leggerla due volte faceva uscire il premio scritto due volte nella
    frase che l'utente legge.
  - `riga`: una riga porta per intero la sua condizione, in una colonna sola.
    Pretende la sola colonna `text`. È la forma che avrebbero BETULLA
    («LINDA SETA … 11+1 Gratis Pz», dentro la descrizione) e NOCE (la
    colonna `descrizione_offerta`, oggi vuota).
  Una parola che il motore non conosce **ferma la lettura e lo dice**: un
  adattatore imparato male non deve leggere «qualcosa comunque».
- **`fields`** — quale colonna dell'adattatore fa da `text`, `reward`, `ean`,
  `row_code`. Sono **nomi di colonna, non lettere**: la posizione si cerca in
  `column_map` (chi le indica per lettera, come LARICE) e poi in
  `header_signature.columns` (chi le indica per nome di intestazione, come
  BETULLA e NOCE). Sta scritta in un posto solo apposta: quando l'utente
  conferma una variazione di schema il registro riscrive quelle, e le
  condizioni seguono i prezzi invece di restare indietro di una settimana.
  Una colonna che il registro non dichiara **non si indovina**: il lettore si
  ferma e nomina in italiano quella che manca.
- **`sheet`** — il nome del foglio, oppure `FIRST` per il primo. Il confronto
  è quello del registro: punti, spazi e maiuscole non contano.
- **`data_start_row`** — da quale riga cominciano i dati (predefinito 1).
- **`max_block_rows`** — solo per `blocchi`: oltre questa distanza
  dall'intestazione il blocco si abbandona, lasciando una condizione
  `da_verificare`. È una difesa contro un listino malformato, non una regola
  commerciale (predefinito 500).
- **`row_markers`** dell'adattatore vale in tutte e due le forme: una riga il
  cui codice è dichiarato `orderable: false` non fa raggiungere la soglia e non
  porta una condizione tutta sua. ⚠ Il fornitore che la riga premio **non la
  marca affatto** la dichiara con `row_markers.reward_rows`: a riconoscerla è
  il testo, con lo stesso giudizio che chiude un blocco
  (`promotions.looks_like_reward`). Serve dal canvass nuovo di LARICE, dove la
  colonna dei marcatori dice `PROMO` su tutta la merce in promozione, premio
  compreso — e il premio, che lì un prezzo ce l'ha, sarebbe diventato l'offerta
  più conveniente di quel prodotto.

Il formato del documento lo decidono i primi byte, non l'estensione: `.xlsx` e
Excel 97-2003 (`.xls`, quello di NOCE) si leggono tutti e due.

**Un fornitore che non dichiara `commercial_conditions` non viene letto e non
produce nessun avviso.** È la differenza fra «non ha condizioni commerciali» e
«non so leggerle»: un avviso che compare a ogni ricalcolo e non chiede di fare
niente non lo legge più nessuno.

### Chi lo dichiara oggi, e perché solo lui

Misurato il **15 agosto 2026** sui quattro listini veri della settimana, cella
per cella:

| Fornitore | Documento | Testi con una parola promozionale | Condizioni vere |
|---|---|---|---|
| LARICE | `33-34.1 07-21 ago.xlsx` | 28 in colonna G | **13 intestazioni di soglia + 13 righe premio** |
| BETULLA | `LISTINO BETULLA … 01-09-26 (1).xlsx` | 12 nella descrizione (D) | 3 confezioni `N+M` già nel prezzo; **7 dei 12 sono la parola «Ogni» di «Ogni Superficie»** |
| CIPRESSO | `Listino3_34.xlsx` | 1 nella descrizione (B) | **nessuna**: è un nome di prodotto che contiene «offerta» |
| NOCE | `formattato_104233.xls` | 5 `N+M GRATIS` nella descrizione (D), 1 dei quali FOOD | **nessuna**: `descrizione_offerta` (P) è vuota su tutte e 18.074 le righe e `offerta` (J) dice `NO` su tutte e 17.148 quelle compilate |

⚠ Dal **4 settembre 2026** i documenti di LARICE sono due, e `commercial_conditions`
la dichiarano tutti e due: `larice_v1` per il canvass senza intestazioni (testo
in G, premio in J) e `larice_canvass_v1` per quello nuovo (testo e premio
tutt'e due in E). Sul nuovo la misura è **6 soglie su 6**. Quale dichiarazione
valga per il documento in mano lo decide l'identificativo che l'ha
riconosciuto, non l'estensione: due schemi dello stesso fornitore possono avere
lo stesso formato.

Per questo `commercial_conditions` la dichiara **solo LARICE**. Accendere la
colonna della descrizione di BETULLA farebbe comparire dodici condizioni di cui
nove non sono condizioni: inventare offerte a chi non ne ha è peggio del
difetto che si stava correggendo. Il giorno che BETULLA ne scrive una davvero, o
che NOCE comincia a compilare `descrizione_offerta`, si aggiunge la voce al
registro — il codice non si tocca.

## Rilevazione dalle fonti

### BETULLA

Passare la descrizione con `included_in_product=True`. Per esempio
`Sheet1!D3230`, “11+1 Gratis”, produce una confezione promozionale. Il prezzo
resta quello del listino: l'unità aggiuntiva è soltanto informativa.

Una coppia `N+M` diventa una confezione promozionale **solo** se nessuna unità
di misura la governa. “ELIDERMA Bagnodoccia 500+100 Omaggio=600 Ml” sono
millilitri e “CUKO ALLUMINIO MT.16+4 GRATIS” sono metri: testi così ricadono
su `detect_ambiguous_offer` e restano `da_verificare`. La grandezza dei numeri
non distingue i due casi — “8+2” è un conteggio giusto sui rasoi e una misura
sbagliata sull'alluminio — quindi il discriminante è l'unità attaccata alla
coppia, prima (`MT.16+4`) o dopo (`500+100 Omaggio=600 Ml`).

### Larice

Le condizioni le legge il ponte da solo, seguendo la `commercial_conditions`
dichiarata dal registro (vedi sopra): niente è cablato nel codice, e chi
chiama direttamente il rilevatore lo fa per costruire una regola a mano.

Per una promozione a blocchi, unire il testo dell'intestazione e quello della
riga omaggio e fornire le righe prodotto ammesse. Per esempio:

```python
detect_threshold_gift(
    supplier="larice",
    source_reference="Canvass!G135:J142",
    source_text="ACQUISTANDO 2 CT TRA; IN OMAGGIO 1 CT DI BIOPUNTO ...",
    eligible={"source_rows": [136, 137, 138, 139, 140, 141]},
)
```

Gli sconti numerici della colonna `P` sono deterministici. Se il prezzo netto
è già stato calcolato in fase di normalizzazione, impostare
`already_applied=True` per impedire una seconda applicazione.

### Noce

Raggruppare le righe che hanno lo stesso testo promozionale nel campo
`availability`; gli EAN o le righe del gruppo diventano `eligible`. Il
rilevatore riconosce anche grafie reali non corrette come `ACQUSITA`.

`LOVEHOME 1+1 OMAGGIO` resta volutamente `offerta_ambigua`: il testo non dice
se si può mescolare la merce né quale articolo viene regalato.

## Calcolo dello stato

`calculate_promotion_state(...)` restituisce:

- `ottenuta`: soglia raggiunta;
- `vicina`: manca al massimo una unità oppure è stato raggiunto almeno
  l'80% della soglia;
- `non_raggiunta`: quantità ancora lontana o nulla;
- `da_verificare`: regola ambigua, non confermata o non collegabile ai prodotti.

`reward_count` indica quante volte è stato ottenuto il premio.

`repeatable` non si legge nel listino: è una condizione del rapporto
commerciale, decisa dall'utente e valida per tutte le soglie. Il rilevatore la
mette sempre a `true`, quindi `reward_count` vale `floor(progresso / soglia)`
e il messaggio dice quanto manca al premio successivo, non che l'omaggio è
stato ottenuto. Dedurla dalla parola “OGNI” significava leggerla in un listino
solo: nei quattro listini Larice misurati “OGNI” non compare mai.

## Decorazione dei dati di confronto

```python
from promotions import decorate_review_data

review_decorata = decorate_review_data(
    review_data,
    promotions,
    selections={
        "product:1": {"quantity": 5, "selectedSupplierId": "noce"}
    },
)
```

La funzione restituisce una copia e aggiunge:

- `promotions` e `promotionSummary` al livello principale;
- `promotions` a ogni prodotto interessato;
- `promotions` a ogni offerta del fornitore interessato;
- `promotionEffectiveUnitPrice` e `promotionEffectiveOrderUnitPrice` soltanto
  per uno sconto numerico confermato, attivo e non già applicato.

I campi originali `selectedSupplierId`, `price`, `unitPriceNet` e
`orderUnitPriceNet` non vengono mai riscritti. Un eventuale ordinamento che
voglia considerare il prezzo promozionale deve usare il campo aggiuntivo solo
quando `economic_effect.affects_supplier_choice` è `true`.

## Uso da riga di comando

```text
python scripts/promotions.py \
  --review-data review_data.json \
  --promotions promotions.json \
  --output review_data_decorata.json
```

Questo passaggio può essere inserito dopo la costruzione dei dati di confronto
oppure eseguito al momento della lettura dello stato utente. Non richiede
modifiche ai listini sorgente.
