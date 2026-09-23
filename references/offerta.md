# Un'offerta: i campi, chi li scrive, chi li legge

## Scopo

Un'offerta è la riga con cui un fornitore dice a che prezzo porta un articolo.
Sta dentro `review_data.json`, in `products[].offers[]`, e la scrive
`display_offer` in `scripts/build_review_data.py`.

Questa scheda esiste perché i campi di un'offerta si leggono in più di trenta
punti del servizio nella forma `x.get("camelCase") or x.get("snake_case")`, e
ogni alias tollerato in più è un nome nuovo che qualcuno domani scriverà al
posto di quello buono. **I nomi qui sotto sono la lista chiusa: non se ne
aggiungono altri.**

## Chi risponde alle domande su un'offerta

`app/offerta.py`, e nessun altro. Tre domande, tre funzioni:

| Domanda | Funzione |
|---|---|
| Di chi è quest'offerta | `offer_supplier_id(offerta)` |
| Si può ordinare | `offer_is_available(offerta)` |
| Quanto costa al pezzo e al collo | `offer_pricing(offerta)` |

Più `find_offer(prodotto, fornitore)`, che pesca l'offerta di un fornitore su
un prodotto — la prima, se il listino la ripete.

⚠ **Non si riscrive nessuna delle tre a mano, nemmeno quando la riga sarebbe di
sette caratteri.** Fino al 20 agosto 2026 `offer.get("available") is not False`
stava scritta a mano in due punti oltre alla sua autorità, e uno dei due era
`pipeline_jobs._ripulisci_stato`, cioè la funzione che decide quali quantità
azzerare dopo un ricalcolo. Due copie della stessa difesa divergono, e quando
divergono qui il sintomo è una quantità che resta su un'offerta che la
compilazione poi rifiuta: si vede una settimana dopo e altrove.

## I campi

| Campo buono | Alias tollerati | Che cos'è |
|---|---|---|
| `supplierId` | `supplier_id` | il fornitore che fa l'offerta |
| `available` | — | `false` = non ordinabile. **Assente vale disponibile**: i listini più vecchi non lo scrivono |
| `quantityFactor` | `unitsPerOrderUnit`, `units_per_order_unit` | quanti pezzi ci sono nell'unità d'ordine. Per un espositore sono i pezzi che contiene, **non 1** |
| `unitPriceNet` | `pricePerPiece`, `price_per_piece` | prezzo al pezzo, netto. È il numero su cui si confrontano fornitori diversi |
| `orderUnitPriceNet` | `price`, `netPrice` | prezzo del collo (o dell'espositore intero), netto |
| `sourceRow` | — | la riga del listino da cui l'offerta viene, e quella in cui si scriverà la quantità |
| `matchStatus` | — | come è stato trovato l'abbinamento, in italiano e per l'utente |
| `requiresConfirmation` | `requires_confirmation` | l'utente deve confermare a mano prima di poter ordinare |

Dei due prezzi ne basta uno: `offer_pricing` ricava l'altro moltiplicando o
dividendo per `quantityFactor`, che se manca vale 1. Se non c'è nessuno dei
due, o se il prezzo del collo è negativo, l'offerta non ha prezzi utilizzabili
e `offer_pricing` risponde `None`.

Un'offerta ne porta altri — `supplierName`, `supplierCode`, `status`, `method`,
`confidence`, `rationale`, `unitPricePreDiscount`, `declaredUnits`,
`components`, `compositionStatus` — che servono a spiegarla in pagina e non a
deciderla. **L'elenco completo e vero è quello che scrive `display_offer`**: se
questa scheda e quella funzione non vanno d'accordo, ha ragione la funzione, e
questa scheda va corretta.

## Chi legge ancora `available` per conto suo, e perché

Due letture non passano da `offer_is_available`, e **non è una dimenticanza**:
rispondono a un'altra domanda, «quale offerta preselezionare», non «si può
ordinare».

- `scripts/build_review_data.py`, `build_products` e la gemella per gli
  espositori: `[offer for offer in offers if offer.get("available")]`. È
  **truthy**, non `is not False`, e le due regole divergono su `available: 0` e
  `available: ""` — il produttore preselezionerebbe «non disponibile» dove
  `offer_is_available` dice «disponibile». Oggi non morde per un motivo
  preciso: `available` lo scrivono solo i due produttori, e lo scrivono solo
  come booleano. Chi un giorno ci mettesse un numero deve sapere che quel
  giorno le due risposte si separano.
- `app/static/app.js`: `offer.available !== false` per ricostruire l'offerta
  in pagina, poi `offer.available` truthy per filtrare. Stessa divergenza,
  stesso motivo per cui oggi non morde. La pagina non può importare
  `app/offerta.py`: se la regola cresce, cresce anche lì, a mano, e va scritto
  qui.

⚠ La prova che sorveglia le copie (`tests/test_offerta.py`,
`test_nessun_altro_file_riscrive_la_regola_a_mano`) guarda **solo** Python e
**solo** le due facce `is False` / `is not False`. Non vede né il truthy né la
pagina: quelle due righe le tiene in piedi questa scheda.

## Le due trappole note

⚠ **Uno `0` lecito viene scambiato per un campo assente.** Le catene con `or`
di `offer_pricing` scartano lo zero insieme al `None`. Oggi non morde per un
motivo che sta altrove: **tutt'e due i produttori di offerte buttano via i
prezzi che non sono `> 0`** prima che arrivino qui — `app/catalog_search.py`,
`_offer`, e `scripts/build_review_data.py`, `display_offer`, tutt'e due dopo lo
stesso guasto («un prezzo letto come 0,00 vince il confronto, perché il più
basso va davanti»). La difesa è a monte e non nel punto che legge: chi togliesse
uno di quei due filtri deve saperlo.

⚠ **`available` assente vale disponibile, e non è un caso.** È l'unico campo
il cui valore mancante non è «non lo so» ma «sì»: i listini letti prima che il
campo esistesse non lo scrivono, e trattarli come non ordinabili renderebbe
vuoto un confronto vecchio.

## Dove si guarda quando qualcosa non torna

- chi produce i campi: `scripts/build_review_data.py`, `display_offer`;
- chi decide che una riga di listino diventa un'offerta:
  `app/catalog_search.py`, `_offer`;
- chi li legge per il confronto e per la compilazione: `app/server.py`;
- chi li legge per azzerare le quantità dopo un ricalcolo:
  `app/pipeline_jobs.py`, `_ripulisci_stato`.
