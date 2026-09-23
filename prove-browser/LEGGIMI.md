# Le prove che premono i pulsanti

I collaudi in `tests/` girano `app.js` con un `addEventListener` finto: provano
che il pulsante ci sia con quel `data-action`, **non che premerlo funzioni**.
Una modifica visibile va guardata nel browser — a mano, finora.

Queste prove guardano nel browser al posto tuo. Non sono una copertura
dell'interfaccia e non devono diventarlo: sono **le sequenze che sono già
costate un difetto**, e ognuna porta in cima la data e il difetto che sorveglia.

## Come si lanciano

```
cd prove-browser
npm install
npx playwright install chromium
npm run prove
```

`npm run prove:vedi` apre il browser e te le fa guardare mentre girano.

## ⚠ Il negozio non c'entra

Questa cartella non fa parte del programma. Il PC del negozio non la installa,
non la esegue e non ne ha bisogno: `requirements.txt` resta di una riga, e la
dipendenza da Node del writer non cambia. Queste prove girano qui e in CI.

## ⚠ Perché sono poche, e devono restare poche

Un collaudo di browser che fallisce per un tempo di attesa invece che per un
difetto insegna a non guardare i rossi. Meglio sei prove che si guardano di
sessanta che si ignorano: se una diventa capricciosa, si aggiusta o si toglie —
non si aggiunge un'attesa più lunga.
