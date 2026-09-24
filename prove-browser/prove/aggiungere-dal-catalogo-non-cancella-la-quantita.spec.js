// @ts-check
import { test, expect } from "@playwright/test";

/**
 * Regression guard: type a new quantity, the save request starts 450 ms
 * later and fails, and in that window a product is added from the catalog.
 * The page re-reads the comparison from the service — which still has the
 * old quantity — and replaces everything on screen with it; the next save
 * then persists the stale value silently, and the order ships wrong.
 *
 * Why this lives here and not in `tests/`: it takes two requests in a row
 * from the same page, with a save that genuinely fails in between. It's the
 * whole sequence, not a single unit.
 *
 * Search and add are stubbed with `page.route`. The synthetic review has no
 * price lists on disk — `synthetic_review()` ships `files: []` — so the
 * service's catalog is empty and there's nothing to add. The bug lives
 * entirely on the page side: what matters is that the add doesn't proceed
 * until the quantity is safely saved, and that a re-read of the comparison
 * doesn't wipe it out.
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

/** Open the catalog dialog and wait for the product to add to actually appear. */
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

    // Starting value must be SAVED, and absolute: tests share state on
    // disk, so without this the value the page would fall back to depends
    // on whichever test ran before it.
    let salvataggio = page.waitForResponse((r) =>
      r.url().includes("/api/state") && r.request().method() === "PUT");
    await quantita.fill("1");
    await quantita.blur();
    expect((await salvataggio).status(), "il valore di partenza non è stato salvato").toBeLessThan(400);

    // From here on saving fails: this is the window the regression lives in.
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

    // The search field is cleared on purpose: once the add succeeds, the
    // page filters the list by the added product's name, and the card under
    // test would disappear. Without this line a failure would read "field
    // not found" instead of "quantity shows 1 where 7 was typed", misleading
    // a future reader into suspecting a stale selector.
    await page.locator("[data-filter='search']").fill("");

    // Check the 7 FIRST — that's the actual damage, the number that would
    // ship on the order. When the bug is present this reads 1, because the
    // re-read comparison overwrote what was on screen; the test must fail on
    // that, not on the message that explains it.
    await expect(quantita).toHaveValue("7");
    // Then the explanation, where the user looks for it: inside the catalog
    // dialog, nowhere else.
    await expect(page.locator(".catalog-dialog").getByText(/non sono ancora salvate/i)).toBeVisible();
    expect(aggiunte, "il prodotto è stato aggiunto con una quantità solo in pagina").toBe(0);

    // With the interception removed, the same action goes through — and the
    // 7 survives with it.
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
