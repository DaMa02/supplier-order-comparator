// @ts-check
import { test, expect } from "@playwright/test";

/**
 * Il difetto del 6 settembre 2026 (R8 della revisione): scrivi 7 al posto di 3,
 * il salvataggio parte 450 ms dopo e non riesce, e in quella finestra aggiungi
 * un prodotto dal catalogo. La pagina rilegge il confronto dal servizio — che
 * ha ancora 3 — e sostituisce tutto quello che hai davanti; il salvataggio
 * successivo consolida il 3. Nessuna parola lo dice, e l'ordine parte sbagliato.
 *
 * Perché sta qui e non in `tests/`: ci vogliono due chiamate di fila dalla
 * stessa pagina, con un salvataggio in mezzo che fallisce per davvero. È la
 * sequenza intera, non il pezzo.
 *
 * ⚠ La ricerca e l'aggiunta si fingono con `page.route`. Il confronto finto
 * non ha listini su disco — `synthetic_review()` porta `files: []` — quindi il
 * catalogo del servizio è vuoto e non c'è niente da aggiungere. Il difetto però
 * sta tutto nella pagina: quello che conta è che il gesto non parta finché la
 * quantità non è al sicuro, e che il confronto riletto non se la porti via.
 */

const PRODOTTO_DEL_CATALOGO = {
  ok: true,
  results: [{
    id: "catalogo-finto",
    name: "PRODOTTO DEL CATALOGO FINTO",
    ean: "8000000000123",
    suppliersCount: 1,
    bestPrice: 2.5,
  }],
};

const comeJson = (corpo, stato = 200) => ({
  status: stato,
  contentType: "application/json",
  body: JSON.stringify(corpo),
});

async function apriIlConfronto(page) {
  await page.goto("/");
  await expect(page.locator("[data-offer-choice='product-standard']").first())
    .toBeVisible({ timeout: 15_000 });
}

/** Apre il catalogo e aspetta che il prodotto da aggiungere ci sia davvero. */
async function apriIlCatalogo(page) {
  await page.locator("[data-action='open-catalog']").click();
  await page.locator("#catalog-search").fill("PRODOTTO DEL CATALOGO");
  await expect(page.locator("[data-action='add-catalog-product']")).toBeVisible();
}

test.describe("Aggiungere un prodotto dal catalogo", () => {
  test("non porta via una quantità che non è ancora salvata", async ({ page }) => {
    let aggiunte = 0;
    await page.route("**/api/products/search*", (rotta) => rotta.fulfill(comeJson(PRODOTTO_DEL_CATALOGO)));
    await page.route("**/api/products/add", (rotta) => {
      aggiunte += 1;
      return rotta.fulfill(comeJson({ ok: true, message: "Prodotto aggiunto all’elenco." }));
    });

    await apriIlConfronto(page);
    const quantita = page.locator("[data-product-quantity='product-standard']");

    // ⚠ Un valore di partenza SALVATO, e assoluto: le prove condividono lo
    // stato sul disco, e senza questo il numero a cui la pagina tornerebbe
    // indietro sarebbe quello lasciato da chi ha girato prima.
    let salvataggio = page.waitForResponse((r) =>
      r.url().includes("/api/state") && r.request().method() === "PUT");
    await quantita.fill("1");
    await quantita.blur();
    expect((await salvataggio).status(), "il valore di partenza non è stato salvato").toBeLessThan(400);

    // Da qui il salvataggio non riesce: è la finestra in cui vive il difetto.
    const salvataggioRotto = (rotta) => (rotta.request().method() === "PUT"
      ? rotta.fulfill(comeJson({ message: "Il servizio locale non risponde." }, 503))
      : rotta.continue());
    await page.route("**/api/state", salvataggioRotto);

    salvataggio = page.waitForResponse((r) =>
      r.url().includes("/api/state") && r.request().method() === "PUT");
    await quantita.fill("7");
    await quantita.blur();
    expect((await salvataggio).status()).toBe(503);

    await apriIlCatalogo(page);
    await page.locator("[data-action='add-catalog-product']").click();

    // ⚠ La ricerca si svuota apposta: quando l'aggiunta va in fondo la pagina
    // filtra l'elenco sul nome del prodotto aggiunto, e la scheda da guardare
    // sparirebbe. Senza questa riga il rosso direbbe «non trovo il campo»
    // invece di «c'è scritto 1 dove avevi scritto 7», e chi lo legge fra sei
    // mesi penserebbe a un selettore invecchiato.
    await page.locator("[data-filter='search']").fill("");

    // ⚠ PRIMA il 7, che è il danno: è il numero che finisce sull'ordine. Con
    // il difetto qui si legge 1 — il confronto riletto dal servizio ha
    // sostituito quello in pagina — e la prova deve cadere su questo, non
    // sulla frase che lo spiega.
    await expect(quantita).toHaveValue("7");
    // Poi il perché, dove l'utente lo cerca: dentro il catalogo, non altrove.
    await expect(page.locator(".catalog-dialog").getByText(/non sono ancora salvate/i)).toBeVisible();
    expect(aggiunte, "il prodotto è stato aggiunto con una quantità solo in pagina").toBe(0);

    // Tolta l'intercettazione, lo stesso gesto arriva in fondo — e il 7 con lui.
    await page.unroute("**/api/state", salvataggioRotto);
    await page.locator("[data-action='close-catalog']").first().click();
    await quantita.fill("7");
    await quantita.blur();
    await expect(page.locator("#save-status")).toHaveText(/Tutto salvato/i);

    await apriIlCatalogo(page);
    await page.locator("[data-action='add-catalog-product']").click();

    await expect(page.locator(".catalog-dialog")).toHaveCount(0);
    expect(aggiunte, "l'aggiunta non è arrivata al servizio").toBe(1);

    await expect(page.locator("#save-status")).toHaveText(/Tutto salvato/i);
    await page.reload();
    await expect(page.locator("[data-offer-choice='product-standard']").first())
      .toBeVisible({ timeout: 15_000 });
    await expect(quantita).toHaveValue("7");
  });
});
