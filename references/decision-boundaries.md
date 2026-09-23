# Confini tra automazione deterministica e AI

## Regola generale

La skill non massimizza il determinismo in astratto. Usa una regola deterministica quando l'operazione ha un risultato corretto definibile, verificabile e ripetibile; usa l'AI quando il dato richiede interpretazione linguistica, conoscenza del prodotto, giudizio commerciale o adattamento a un'interfaccia variabile.

## Matrice operativa

| Attività | Approccio | Motivo |
| --- | --- | --- |
| Inventario, percorsi e archiviazione | Deterministico | Evita perdite o spostamenti fuori cartella. |
| Riconoscimento degli schemi già noti | Deterministico | Le intestazioni e le colonne sono documentate. |
| Scansione delle righe attive | Deterministico | Deve coprire l'intero worksheet senza dipendere da anteprime o tabelle. |
| Normalizzazione EAN e match EAN | Deterministico | Gli identificatori non richiedono interpretazione semantica. |
| Prezzi, sconti, IVA, confezioni, totali e soglia | Deterministico | Sono calcoli auditabili. |
| EAN duplicato | Ibrido | Il motore conserva tutte le righe attive e calcola gli attributi; l'AI valuta la variante perché lo stesso codice può essere condiviso da prodotti commercialmente diversi. |
| EAN assente | Ibrido | Shortlist e incompatibilità strutturate sono ripetibili; equivalenza commerciale e linguistica richiede giudizio. |
| Nuovo fornitore non conosciuto | Ibrido | L'AI propone la mappatura, l'utente la conferma, poi l'adattatore diventa deterministico. |
| Riconoscimento espositore | Ibrido a regole forti | Il motore riconcilia struttura, quantità e prezzi; l'AI o l'utente decide i casi incompleti. |
| Confronto di espositori | Deterministico se composizione completa, altrimenti umano | Un fingerprint EAN+quantità prova l'equivalenza; nomi simili non bastano. |
| Anomalie e brief | AI su evidenze | L'AI spiega e ordina le eccezioni, senza ricalcolare i dati. |
| Proposte per superare EUR 1.000 | Ibrido | Il motore calcola costi e combinazioni; l'AI presenta le alternative commercialmente sensate. |
| Scrittura quantità nei listini | Deterministico | Usa righe sorgente approvate, senza rifare il matching. |
| Quantità, scelta fornitore e soglie | UI locale + controlli deterministici | L'utente lavora in una pagina semplice; il backend ricalcola e valida lo snapshot. |
| Navigazione Noce | AI controllata | L'interfaccia può cambiare; prima e dopo ogni azione si riconciliano prodotto e quantità con il piano. |
| Invio definitivo dell'ordine | Umano | La skill non lo esegue mai. |

## Criterio di escalation

Una decisione AI deve poter terminare con `RIFIUTATO` o `DA_VERIFICARE`; non è obbligata a scegliere un prodotto. Marca, formato, quantità, forma e variante incompatibili prevalgono sulla somiglianza testuale. Le decisioni semantiche accettate rimangono visibili nel comparativo e richiedono conferma prima della preparazione dell'ordine.
