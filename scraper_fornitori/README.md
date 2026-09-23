# Scraper dei siti fornitori — lo scheletro

Questa cartella **non contiene nessuno scraper funzionante**, e non è collegata
al comparatore: è la metà generica del lavoro, tenuta da parte per il giorno in
cui un fornitore non manderà più il suo listino e l'unico modo per avere i
prezzi sarà il suo sito.

Oggi ogni fornitore manda un file — `.xlsx`, `.xls` o `.csv` — e il file si
carica dal passo 1 della pagina. Finché è così, qui non si tocca niente.

## Che cosa c'è già, e che cosa manca

| File | Che cos'è |
|---|---|
| `scraper_fornitore.py` | il motore: sessione HTTP con i cookie, tentativi ripetuti, ripresa dopo un'interruzione, scrittura incrementale del CSV, `metadata.json`. **Non si tocca** quando si aggiunge un fornitore |
| `modello_fornitore.py` | il modello da copiare: le tre sole cose che dipendono dal sito |
| `valida_catalogo.py` | guarda il file scaricato e decide se è utilizzabile |
| `controllo_a_campione.py` | ripesca sul sito qualche articolo e lo confronta con il CSV |

Le tre cose che dipendono dal sito, e che nessuno può scrivere al posto tuo:

1. **come si accede** — quale form, quali campi, e soprattutto *da cosa si
   capisce che l'accesso è riuscito*;
2. **quante pagine ci sono** — letto dal sito a ogni esecuzione, mai un numero
   scritto a mano;
3. **come si legge una pagina** — quale indirizzo restituisce le righe, e quale
   cella è quale colonna.

## Come si aggiunge un fornitore

```bash
cd scraper_fornitori
cp modello_fornitore.py scraper_nomefornitore.py
```

Poi, prima di scrivere una riga di codice, apri il sito col browser e guarda
come funziona davvero:

1. arriva all'elenco dei prodotti;
2. apri gli strumenti per sviluppatori, scheda **Rete**, e cambia pagina
   nell'elenco. Quasi sempre la pagina non si ricarica tutta: chiama un
   indirizzo che restituisce le sole righe. **Quello** è l'indirizzo da
   chiamare — guidare il browser attraverso duecento pagine è lento, fragile e
   inutile;
3. guarda che cosa viene inviato al login, campi nascosti compresi;
4. guarda che cosa cambia nell'indirizzo delle righe quando cambi pagina: di
   solito è un numero d'inizio e uno di fine, non il numero di pagina.

Poi riempi i tre metodi e prova in piccolo:

```bash
python scraper_nomefornitore.py --output ./output-prova --max-pagine 2 --reset
```

Quando le due pagine di prova sono giuste, il giro intero:

```bash
python scraper_nomefornitore.py --output ./output --reset
python scraper_nomefornitore.py --output ./output --riprendi   # dopo un'interruzione
```

`--riprendi` riparte dalla pagina dopo l'ultima completata, che sta in
`ripresa.json`. `--reset` cancella soltanto i file dentro la cartella indicata
da `--output`.

## I controlli, che non sono facoltativi

Uno scraper che arriva in fondo senza errori **non** vuol dire che il catalogo
sia buono: la sessione può scadere a metà, una pagina può tornare vuota, il
listino può cambiare mentre lo si scarica. Prima di usare il file:

```bash
python valida_catalogo.py \
  ./output/catalogo_nomefornitore.csv \
  ./output/catalogo_pulito.csv \
  ./output/rapporto_qualita.json \
  --metadati ./output/metadata.json

python controllo_a_campione.py ...   # si chiama dal tuo scraper: vedi sotto
```

`valida_catalogo.py` guarda **il file**: pagine mancanti, righe senza
descrizione o senza prezzo leggibile, duplicati, e lo scarto fra i conteggi
dichiarati dal sito prima e dopo. Uno scarto fino a 20 articoli passa con un
avviso — il catalogo può essere cambiato durante l'estrazione — da 21 in su
blocca.

`controllo_a_campione.py` guarda **il sito**: ripesca cinque articoli
distribuiti nel catalogo e pretende che tornino tutti identici. Gli serve
l'oggetto del sito, quindi si chiama dal proprio scraper:

```python
from controllo_a_campione import avvia
from scraper_nomefornitore import SitoNomeFornitore

raise SystemExit(avvia(SitoNomeFornitore()))
```

## Le credenziali

Non stanno nel codice, non stanno negli argomenti della riga di comando, non
stanno in un file dentro il repository, e non finiscono negli output. Lo
scheletro le legge da due variabili d'ambiente — `<NOME>_UTENTE` e
`<NOME>_PASSWORD`, dove `<NOME>` è il campo `nome` del sito in maiuscolo —
oppure le chiede a schermo, con la password nascosta.

La riga di comando finisce nella cronologia della shell, nei log
dell'Utilità di pianificazione e nell'elenco dei processi: per questo la
password non è un argomento, in nessun caso.

## Se un giorno serve davvero collegarlo al comparatore

Questo è il punto che va deciso prima di scrivere il codice, non dopo.

Il programma accetta dal passo 1 della pagina `.xlsx`, `.xls` **e `.csv`**, e
per i CSV ha un lettore generico guidato dal registro degli adattatori
(`read_mapped_csv_supplier` in `scripts/prepare_manifest_sources.py`): quale
colonna è l'EAN, quale il prezzo, dove cominciano i dati, quali righe si
scartano. Quindi il file che esce di qui si può caricare come qualunque altro
listino, dichiarando un adattatore — **senza scrivere codice nuovo nel
programma**.

Quello che resta da decidere non è tecnico: quanto ci si fida di un listino che
nessun fornitore ha firmato. Un file mandato dal fornitore è la sua parola; un
file costruito leggendo il suo sito è la nostra lettura del suo sito, e un
cambio di pagina che sposta una colonna diventa un prezzo sbagliato in un
ordine vero. È il motivo per cui `valida_catalogo.py` e
`controllo_a_campione.py` stanno qui dentro e non sono facoltativi.

Vale comunque questa regola: le convenzioni di un fornitore —
quali colonne, quali righe si scartano, quale colonna d'ordine — stanno nel
registro degli adattatori, non in un `if fornitore == "..."` dentro lo scraper.
Lo scraper porta i dati, il registro dice che cosa sono.

## Quello che non si fa

- **Niente dipendenze nuove.** Lo scheletro usa la sola libreria standard, come
  il resto del progetto. Un sito che si può leggere solo con un browser
  automatizzato è una scelta di architettura, da discutere prima.
- **Niente parallelismo sullo scaricamento.** Una sola sessione, una pagina per
  volta: i gestionali web tengono lo stato dell'elenco nella sessione, e due
  richieste insieme si rubano il posto a vicenda — il risultato non è un errore
  ma pagine duplicate o saltate, che è peggio. Il controllo a campione usa
  quattro sessioni separate, e quello va bene perché ognuna riparte dal login.
- **Niente numeri di pagina scritti a mano.** Il catalogo cresce, e uno scraper
  che si ferma a un numero fisso smette di prendere la coda senza dirlo.
