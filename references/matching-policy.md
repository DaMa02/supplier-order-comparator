# Politica di matching

## Indice

1. Ordine delle operazioni
2. EAN esatto
3. Shortlist deterministica
4. Valutazione AI
5. Espositori tra fornitori
6. Stati e audit

## 1. Ordine delle operazioni

Applicare nell'ordine:

1. normalizzazione deterministica degli identificatori;
2. match EAN esatto;
3. raccolta completa e confronto strutturato delle occorrenze duplicate;
4. generazione deterministica di una shortlist semantica;
5. valutazione AI della shortlist e dei casi EAN duplicati rimasti ambigui;
6. revisione umana dei risultati non certi.

## 2. EAN esatto

Usare l'EAN gestionale come source of truth. Eliminare soltanto spazi esterni e artefatti numerici Excel come `.0`; non rimuovere caratteri interni e non convertire arbitrariamente codici non standard.

Leggere valori di celle attive. Una stringa presente soltanto nel dizionario interno `sharedStrings.xml` non è un prodotto del listino.

Stati:

- `EAN_ESATTO`: una sola riga utilizzabile;
- `EAN_AMBIGUO`: più righe ancora valide;
- `EAN_ASSENTE`: nessuna riga.

## 3. Shortlist deterministica

Per ogni `EAN_ASSENTE`, normalizzare descrizioni, marca e quantità/formato. Estrarre quando presenti:

- marca e famiglia;
- volume o peso (`ml`, `l`, `g`, `kg`);
- numero di pezzi, rotoli, lavaggi o notti;
- variante, profumazione, colore, taglia e pubblico;
- forma prodotto, per esempio spray, ricarica, roll-on o gel.

Le quantità si leggono in tutte le forme che i listini usano davvero (`build_semantic_shortlists.attributes`, dal 21 settembre 2026):

- unità dopo il numero: `18PZ`, `500 ML`, `3LT`, `250GR`, `78 MISURINI` (lavaggi);
- unità davanti: `X 18`, `PZ.18`, `ML.500`, `LT.3`, `KG 4`;
- somme attaccate, lette come base **e** come totale: `70+8 LAV` vale 70 e 78, `PZ.8+2` vale 8 e 10;
- una `L` sola dopo un intero da 10 in su sono lavaggi (`COCCOLONE 45L`); `LT` e `LITRI` restano litri;
- con un numero subito prima: l'unità col punto attaccato è sempre del numero dopo (`X 2 GR.90` sono 90 g, `4 IN 1 GR.900` sono 900 g); punto e spazio (`GR. 500`) solo se il numero prima conta pezzi; il solo spazio (`PH 3.5 ML 200`) solo per volumi e pesi, e se il numero dopo non ha un'unità sua;
- pezzi e peso (o volume) nello stesso nome valgono anche come totale: `GR.250 X 2` vale 250 e 500 g.

Non si leggono, di proposito: le fasce (`11-25 KG` del bambino, `0-6` anni, `KG. 11/25`), le taglie (`5°MIS.`), le formule (`2X13=26`), la `X` seguita da un numero con la sua unità (`54 DOSI X 12=648 GR`: 12 sono i grammi di una dose), le misure (`30 X 40 CM`), la `X` dopo un numero o prima di un'unità senza un numero suo (`2 X 250ML` vale 250 ML), i numeri attaccati a lettere (`2IN1`), le età e le protezioni (`45+`, `FP 50+`), i codici (`E1`, `N.4`), i `+` staccati. Meglio non leggere un numero che leggerlo sbagliato: un falso conflitto toglie 0,35 alla riga giusta.

Penalizzare deterministicamente i candidati con formato numerico incompatibile e ordinare i rimanenti con un punteggio riproducibile basato su token, string similarity e attributi strutturati. Nei file attuali la marca non ha una colonna separata affidabile: farla interpretare all'AI dalla descrizione, invece di inventare una regola rigida. Passare all'AI soltanto i migliori candidati e i relativi punteggi.

## 4. Valutazione AI

L'AI deve valutare candidati già prodotti, non cercare liberamente nell'intero listino. Può rifiutare tutti i candidati o lasciare il caso irrisolto: la somiglianza testuale non impone un match.

Rifiutare il match se:

- la marca è diversa senza un alias documentato;
- volume, peso, numero di pezzi o multipack sono incompatibili;
- la variante modifica sostanzialmente il prodotto;
- il miglior candidato non è nettamente preferibile al secondo.

Un match semantico accettato deve riportare una motivazione breve e un livello di confidenza. I match medi o bassi richiedono revisione dell'utente prima dell'ordine.

## 5. Espositori tra fornitori

Confrontare due espositori come offerte dello stesso articolo soltanto quando la composizione è dimostrabilmente equivalente. La chiave primaria è un fingerprint stabile formato dalle coppie ordinate `EAN componente + quantità`. Il nome del display, il numero totale di pezzi o la sola marca non bastano.

Se tutti gli EAN e le quantità coincidono, le offerte sono comparabili anche quando i fornitori formulano diversamente il nome. Se mancano EAN o quantità, proporre il confronto solo con motivazione e conferma umana. Non confrontare automaticamente il prezzo per pezzo di due composizioni diverse.

## 6. Stati e audit

Conservare per ogni prodotto-fornitore:

- EAN gestionale;
- EAN e riga del candidato;
- metodo del match;
- punteggio deterministico;
- candidati alternativi;
- decisione AI e motivazione, se applicabile;
- conferma o correzione dell'utente.

Per gli espositori conservare inoltre riga padre, righe componente, quantità dichiarata e ricostruita, prezzo padre e ricostruito, confidenza, evidenze e fingerprint della composizione.

Non rieseguire il matching nella fase di scrittura dell'ordine: utilizzare la riga sorgente approvata e persistita.

### Lo stesso codice presso un altro fornitore (`EAN_DA_ALTRO_FORNITORE`)

Dal 21 settembre 2026 (`merge_match_decisions.propaga_lo_stesso_codice`): se presso il fornitore A l'AI ha accettato una riga con EAN X diverso da quello del gestionale, presso ogni altro fornitore S **senza** abbinamento e con stato `EAN_ASSENTE`, l'unica riga utilizzabile con EAN X entra con `status: SEMANTICO_PROPOSTO`, `method: EAN_DA_ALTRO_FORNITORE`, confidenza `MEDIA` e **sempre da confermare**. Porta `propagato_da` (i fornitori fonte), `codice_propagato` e `prima` (stato, metodo, confidenza e motivazione che sostituisce).

- Scavalca un `REJECT` dell'AI presso S e un `DA_VERIFICARE` non scartato: è il caso da cui nasce (le Linda Seta x18, rifiutate presso LARICE e accettate presso NOCE con lo stesso codice).
- Non scavalca mai una riga già scelta, una decisione scartata (`ai_decisione_scartata`) né uno stato EAN diverso da `EAN_ASSENTE`.
- Due o più righe candidate presso S (lo stesso EAN ripetuto, o due fonti con codici diversi): non si propone niente, si conta in `stesso_codice_ambiguo`.
- Solo codici con la forma di un EAN vero (8, 12, 13 o 14 cifre, non tutti zeri), confrontati grezzi come la strada nativa.
- Le fonti sono i soli `SEMANTICO_AI`: una riga propagata non diventa fonte, e l'ordine dei fornitori non cambia il risultato.
- Un «no» dell'utente sulla riga propagata la spegne per impronta come ogni altro rifiuto; un «sì» si ricorda come conferma di (S, prodotto) con motivo `EAN_DA_ALTRO_FORNITORE`. Nessuna uguaglianza fra codici si scrive da sola.
