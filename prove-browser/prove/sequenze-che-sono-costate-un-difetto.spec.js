// @ts-check
import { test, expect } from "@playwright/test";

/**
 * Le sequenze che il 26 agosto 2026 hanno prodotto difetti veri.
 *
 * ⚠ Ognuna porta il difetto che sorveglia. Una prova di browser senza quella
 * riga, fra sei mesi, è una prova che nessuno sa se può togliere.
 *
 * Quello che questi collaudi provano e che quelli in `tests/` non possono
 * provare: che premere il pulsante FUNZIONI. Là il gestore è registrato su un
 * `addEventListener` finto — si prova che il pulsante ci sia con quel
 * `data-action`, non che il giro completo arrivi in fondo.
 */

/** La pagina è pronta quando il confronto è a schermo, non quando ha risposto. */
async function apriIlConfronto(page) {
  await page.goto("/");
  await expect(page.locator("[data-offer-choice='product-standard']").first())
    .toBeVisible({ timeout: 15_000 });
}

/** Il fornitore selezionato in quella riga, letto dalla scelta accesa. */
async function fornitoreScelto(page, prodotto) {
  return page.locator(`[data-offer-choice='${prodotto}']:checked`).getAttribute("value");
}

test.describe("Il confronto si apre", () => {
  test("la pagina mostra le offerte dei due fornitori", async ({ page }) => {
    await apriIlConfronto(page);

    const scelte = page.locator("[data-offer-choice='product-standard']");
    await expect(scelte).toHaveCount(2);
  });

  test("parte dal fornitore più conveniente", async ({ page }) => {
    // NOCE sta a 1,00 €/pz contro 1,50 di LARICE.
    await apriIlConfronto(page);

    expect(await fornitoreScelto(page, "product-standard")).toBe("noce");
  });
});

test.describe("Le sequenze di due chiamate di fila", () => {
  /**
   * Il difetto del 26 agosto 2026: lo sconto avanza la versione dello stato sul
   * disco e la sua risposta non la riportava indietro. La scheda restava
   * indietro di uno, e il PRIMO salvataggio successivo — fatto da lei stessa —
   * si sentiva rispondere «un'altra scheda del comparatore ha salvato dopo di
   * te», con l'utente mandato a cercare una scheda che non esisteva.
   *
   * È la sequenza che un collaudo di unità non vede: ci vogliono due chiamate
   * di seguito dalla stessa pagina.
   */
  test("dopo un «non è lo stesso articolo» il salvataggio non viene rifiutato", async ({ page }) => {
    await apriIlConfronto(page);

    // ⚠ ONESTÀ SU QUELLO CHE QUESTA PROVA È E NON È.
    //
    // Vorrebbe sorvegliare il difetto del 26 agosto 2026 — il salvataggio
    // rifiutato con «un'altra scheda del comparatore ha salvato dopo di te»
    // quando di schede ce n'era una sola. **Non ci riesce**: rimettendo il
    // difetto (via `stateVersion` dai tre `return` di `server.py`, e anche
    // togliendo `applyStateVersion` da `requestJson`) questa prova resta
    // VERDE. Provato, non supposto.
    //
    // Il motivo per cui non l'ho ancora inchiodata: le strade che riallineano
    // la versione sono più di una — lo sconto, per esempio, chiama
    // `loadReview()` dopo la sua risposta e si rimette in pari da solo, quindi
    // non era nemmeno lui a produrre il difetto — e non sono riuscito a
    // isolare in browser la sequenza esatta che lo produceva.
    //
    // Quello che allora prova davvero: che il giro completo rifiuto →
    // salvataggio arrivi in fondo con un 2xx e la pagina dica «Tutto salvato».
    // È una guardia del giro, non la sentinella di quel difetto. Chi riesce a
    // farla diventare rossa rimettendo il difetto, cambi questo commento.
    const rispostaRifiuto = page.waitForResponse((r) =>
      r.url().includes("/api/matches/rifiuta") && r.request().method() === "POST");
    await page.locator("[data-action='rifiuta-abbinamento'][data-product-id='product-da-confermare']").first().click();
    await rispostaRifiuto;

    // Poi un gesto qualunque che faccia salvare. Il salvataggio è differito di
    // 450 ms: si aspetta la SUA risposta, non il click.
    const rispostaSalvataggio = page.waitForResponse((r) =>
      r.url().includes("/api/state") && r.request().method() === "PUT");
    await page.locator("[data-action='change-quantity'][data-product-id='product-standard'][data-delta='1']").click();
    const esito = await rispostaSalvataggio;

    expect(esito.status(), "il salvataggio è stato rifiutato").toBeLessThan(400);
    await expect(page.locator("#save-status")).toHaveText(/Tutto salvato/i);
    await expect(page.getByText(/un'altra scheda del comparatore/i)).toHaveCount(0);
  });

  test("uno sconto riporta la scelta sul fornitore che costa meno", async ({ page }) => {
    await apriIlConfronto(page);

    // −40% su LARICE lo porta a 0,90 €/pz, sotto NOCE a 1,00.
    await page.locator("[data-supplier-discount='larice']").fill("40");
    await page.locator("[data-supplier-discount='larice']").blur();

    await expect
      .poll(() => fornitoreScelto(page, "product-standard"), { timeout: 10_000 })
      .toBe("larice");
  });
});

test.describe("La quantità", () => {
  /**
   * Il difetto del 26 agosto 2026, introdotto correggendo quello del fornitore:
   * il ramo «l'ha scelto l'utente» usciva dal ciclo con un `continue` e saltava
   * il ripristino di `quantitySource`. Con fornitore scelto a mano E quantità
   * scritta a mano, dopo un ricaricamento la quantità tornava a dichiararsi
   * «valore dal gestionale».
   */
  test("scelto un fornitore a mano, la quantità sopravvive al ricaricamento", async ({ page }) => {
    await apriIlConfronto(page);

    // Il fornitore più caro, scelto apposta: è una decisione, e va difesa.
    await page.locator("[data-offer-choice='product-standard'][value='larice']").check();
    // ⚠ Un valore assoluto, non due click su «+»: le prove condividono lo
    // stesso stato sul disco, e contare i click rende il risultato dipendente
    // da chi ha girato prima. È già successo — questa prova passava da sola e
    // falliva in fila, trovando 3 dove si aspettava 2.
    await page.locator("[data-product-quantity='product-standard']").fill("2");
    await page.locator("[data-product-quantity='product-standard']").blur();

    await expect(page.getByText(/Tutto salvato/i).first()).toBeVisible({ timeout: 10_000 });
    await page.reload();
    await expect(page.locator("[data-offer-choice='product-standard']").first()).toBeVisible({ timeout: 15_000 });

    expect(await fornitoreScelto(page, "product-standard")).toBe("larice");
    // ⚠ E la quantità non deve essere tornata «valore dal gestionale»: era
    // quello il difetto: da lì il comando che azzera le sole quantità
    // predefinite se la sarebbe portata via.
    await expect(page.locator("[data-product-quantity='product-standard']")
      .locator("xpath=ancestor::*[contains(@class,'product-card')][1]")
      .getByText(/valore dal gestionale/i)).toHaveCount(0);
    await expect(page.locator("[data-product-quantity='product-standard']")).toHaveValue("2");
  });
});

test.describe("La riga proposta da un fornitore scartato", () => {
  /**
   * Il difetto del 4 settembre 2026, visto sul PC del negozio su LESTOX LEGNO
   * PULITO 5IN1: la pagina diceva due cose opposte sullo stesso fornitore a due
   * centimetri di distanza — la sua riga proposta con EAN, codice e prezzo, e
   * sotto la tabella «non ce l'hanno nel listino di adesso».
   *
   * Quello che le prove in `tests/` non possono provare, e che sta qui: che la
   * contraddizione non torni DOPO la risposta. Il no toglie l'avviso e lascia
   * l'offerta non disponibile, cioè esattamente la condizione che rimetteva
   * quel fornitore nella frase. Ci vuole il giro completo: click, risposta del
   * servizio, confronto riletto, pagina ridisegnata.
   */
  const scheda = (page) =>
    page.locator("[data-product-quantity='product-con-proposta']")
      .locator("xpath=ancestor::*[contains(@class,'product-card')][1]");

  test("chi propone una riga non è anche uno che «non ce l'ha»", async ({ page }) => {
    await apriIlConfronto(page);
    const carta = scheda(page);

    await expect(carta.getByText("PRODOTTO CON RIGA PROPOSTA 2PZ")).toBeVisible();
    await expect(carta.getByText(/non ce l’hanno nel listino di adesso/)).toHaveCount(0);
  });

  test("e il motivo dello scarto si legge senza aprire niente", async ({ page }) => {
    await apriIlConfronto(page);

    await expect(scheda(page).getByText(/confezione da 2 pezzi/)).toBeVisible();
  });

  test("nemmeno dopo aver risposto «No, non è lo stesso»", async ({ page }) => {
    await apriIlConfronto(page);
    const carta = scheda(page);

    const risposta = page.waitForResponse((r) =>
      r.url().includes("/api/matches/answer") && r.request().method() === "POST");
    await carta.locator("[data-action='answer-candidate'][data-accepted='false']").click();
    const esito = await risposta;

    expect(esito.status(), "la risposta è stata rifiutata").toBeLessThan(400);
    await expect(carta.getByText(/NON è lo stesso articolo/)).toBeVisible({ timeout: 10_000 });
    await expect(carta.getByText(/non ce l’hanno nel listino di adesso/)).toHaveCount(0);
  });
});
