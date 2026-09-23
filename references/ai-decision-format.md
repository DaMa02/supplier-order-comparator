# Formato delle decisioni AI

Salvare le decisioni in JSON come lista. Ogni elemento deve contenere:

```json
{
  "gestionale_source_row": 12,
  "supplier": "betulla",
  "action": "ACCEPT",
  "source_row": 441,
  "confidence": "ALTA",
  "rationale": "Marca e formato coincidono; descrizione abbreviata nel listino.",
  "requires_user_confirmation": true,
  "ai_impronta_caso": "9d3f0c1a7b2e4056",
  "ai_articolo_mostrato": "PANTERA SHAMPOO 250ML RICCI NEW",
  "ai_modello": "openai/gpt-5.6-luna",
  "ai_versione_prompt": "v3",
  "ai_versione_avversario": "v1"
}
```

Valori ammessi:

- `action`: `ACCEPT`, `REJECT`, `UNRESOLVED`;
- `supplier`: `betulla`, `larice`, `noce` o un adattatore registrato;
- `confidence`: `ALTA`, `MEDIA`, `BASSA`;
- `source_row`: obbligatoria solo per `ACCEPT` e deve appartenere **alla shortlist di quella coppia**, cioè all'insieme dei candidati che sono stati mostrati al modello.

## `ai_impronta_caso` — obbligatoria

Le prime sedici cifre dell'impronta del caso **come il modello l'ha visto**: riga e fornitore del gestionale, descrizione dell'articolo cercato e l'elenco ordinato dei candidati con `source_row`, descrizione e punteggio. La calcola `build_semantic_shortlists.impronta_caso`, la scrive `scripts/valuta_shortlist.py` e la verifica `scripts/merge_match_decisions.py` ricalcolandola dalla shortlist della run corrente.

Serve contro un caso che nessun altro controllo vede: **un file di decisioni di un'altra run**. Il gestionale è lo stesso file di settimana in settimana, quindi le coppie `(riga, fornitore)` si sovrappongono quasi tutte, i conteggi riconciliano e le altre guardie confrontano fra loro artefatti che sono tutti della run corrente. L'impronta è l'unico campo che porta con sé che cosa il modello aveva davanti.

Comprende **tutti** i candidati, non solo quello accettato: un `REJECT` vecchio dice «nessuno di questi va bene» a proposito di un elenco che oggi è un altro, e quel prodotto sparisce dal confronto presso quel fornitore senza che niente lo segnali.

Una decisione la cui impronta non corrisponde — o che non la porta affatto — non viene applicata: quella coppia torna a `DA_VERIFICARE` e `merge_match_decisions.py` esce con **3**. Le due cose restano distinte nella causa e nel messaggio, perché mandano a cercare il guasto in due posti diversi: `DECISIONE_DI_UNA_ALTRA_RUN` per un'impronta che non corrisponde, `DECISIONE_SENZA_IMPRONTA` per una che non c'è. L'avviso in cima alla pagina le conta tutte e due. Ammettere una decisione senza impronta renderebbe la guardia aggirabile dimenticandosi un campo.

⚠ **Che cosa l'impronta prova, e che cosa no.** Prova che il caso valutato è **identico** a quello di oggi. Se il listino di un fornitore non cambia da una settimana all'altra, il caso è davvero lo stesso: l'impronta combacia e la decisione della settimana scorsa passa — ed è giusto, perché è quello che fa apposta la memoria delle risposte in `app/data/memoria_ai.json`. Quello che l'impronta **non** dice è con che cosa quella decisione è stata presa.

## `ai_modello`, `ai_versione_prompt`, `ai_versione_avversario` — obbligatorie

La provenienza. Servono per il caso che l'impronta lascia passare: una decisione presa con il prompt `v1` — che sbagliava 5 `ALTA` su 400 casi e non aveva la verifica avversariale — su un caso che nel frattempo non è cambiato. Le scrive `scripts/valuta_shortlist.py` dalla configurazione viva.

`merge_match_decisions.py` le **legge e non le giudica**: la configurazione di oggi non ce l'ha. Il confronto è lavoro dell'orchestratore, che ce l'ha, e sta fra le cose dichiarate e non fatte della 6b.

`ai_articolo_mostrato` non viene verificata: porta la descrizione dell'articolo su cui la decisione è stata presa perché chi legge il file o il riepilogo capisca di che cosa si stava parlando.

Le versioni precedenti ammettevano anche «i candidati EAN ambigui», cioè le righe di `usable_candidates` in `matching_result.json`. **Non vale più**: quelle righe al modello non arrivano — il passo AI costruisce i suoi casi dalle sole shortlist — e una riga ammessa ma mai mostrata è una porta aperta su un `ACCEPT` che nessuno ha davvero valutato. Una `source_row` fuori dalla shortlist fa uscire `merge_match_decisions.py` con **5** e degrada quella coppia a `DA_VERIFICARE`.

Non inventare righe, EAN o prezzi. La motivazione deve citare gli attributi che sostengono o impediscono il match. Se due candidati restano equivalenti, usare `UNRESOLVED`.

