// @ts-check
import { test, expect } from "@playwright/test";

/**
 * Click sequences that each guard against a real regression.
 *
 * Each test states the regression it guards against, so a browser test
 * without that context is one nobody can tell is safe to delete.
 *
 * What these tests prove, and unit tests in `tests/` can't: that clicking
 * the button actually WORKS end to end. There, the handler is attached to a
 * fake `addEventListener` — proving the button exists with that
 * `data-action`, not that the full round trip completes.
 */

/** The page is ready once the comparison is rendered, not once the request resolves. */
async function apriIlConfronto(page) {
  await page.goto("/");
  await expect(page.locator("[data-offer-choice='product-standard']").first())
    .toBeVisible({ timeout: 15_000 });
}

/** The supplier selected for that row, read from the checked radio choice. */
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
    // NOCE is 1.00 €/pc against LARICE's 1.50.
    await apriIlConfronto(page);

    expect(await fornitoreScelto(page, "product-standard")).toBe("noce");
  });
});

test.describe("Le sequenze di due chiamate di fila", () => {
  /**
   * A past regression: the discount action advanced the state version on
   * disk, but its response didn't report the new version back to the page.
   * The page stayed a version behind, and the next save from that same page
   * was rejected as a conflict with a phantom other tab.
   *
   * This is a sequence a unit test can't see: it takes two requests in a
   * row from the same page.
   */
  test("dopo un «non è lo stesso articolo» il salvataggio non viene rifiutato", async ({ page }) => {
    await apriIlConfronto(page);

    // What this test does and doesn't prove.
    //
    // The intent was to guard the regression above directly — a save
    // rejected as a version conflict with only one tab open. It doesn't:
    // reintroducing the bug (by dropping `stateVersion` from `server.py`'s
    // three `return` points, and removing `applyStateVersion` from
    // `requestJson`) leaves this test green. Verified, not assumed.
    //
    // More than one code path re-syncs the version — the discount action,
    // for instance, calls `loadReview()` after its own response and
    // resyncs on its own, so it wasn't even the one causing the original
    // bug — and the exact sequence that triggers it hasn't been isolated
    // in the browser.
    //
    // What this test actually proves: that the full reject → save round
    // trip completes with a 2xx and the page reports "Tutto salvato". It's
    // a guard on that flow, not a sentinel for the specific regression.
    // Whoever manages to make it fail by reintroducing the bug should
    // update this comment.
    const rispostaRifiuto = page.waitForResponse((r) =>
      r.url().includes("/api/matches/rifiuta") && r.request().method() === "POST");
    await page.locator("[data-action='rifiuta-abbinamento'][data-product-id='product-da-confermare']").first().click();
    await rispostaRifiuto;

    // Then any action that triggers a save. Saving is debounced by 450 ms:
    // wait for the save's own response, not the click.
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

    // −40% on LARICE brings it to 0.90 €/pc, below NOCE's 1.00.
    await page.locator("[data-supplier-discount='larice']").fill("40");
    await page.locator("[data-supplier-discount='larice']").blur();

    await expect
      .poll(() => fornitoreScelto(page, "product-standard"), { timeout: 10_000 })
      .toBe("larice");
  });
});

test.describe("La quantità", () => {
  /**
   * A past regression, introduced while fixing the supplier-selection one
   * above: the "user picked this supplier" branch exited its loop with a
   * `continue` and skipped restoring `quantitySource`. With both a
   * manually chosen supplier AND a manually typed quantity, a reload would
   * make the quantity report itself as "valore dal gestionale" again.
   */
  test("scelto un fornitore a mano, la quantità sopravvive al ricaricamento", async ({ page }) => {
    await apriIlConfronto(page);

    // The more expensive supplier, chosen deliberately: it's a decision,
    // and it must be preserved.
    await page.locator("[data-offer-choice='product-standard'][value='larice']").check();
    // An absolute value, not two clicks on "+": tests share the same state
    // on disk, so counting clicks makes the result depend on whichever test
    // ran before it, passing in isolation and failing when run in sequence.
    await page.locator("[data-product-quantity='product-standard']").fill("2");
    await page.locator("[data-product-quantity='product-standard']").blur();

    await expect(page.getByText(/Tutto salvato/i).first()).toBeVisible({ timeout: 10_000 });
    await page.reload();
    await expect(page.locator("[data-offer-choice='product-standard']").first()).toBeVisible({ timeout: 15_000 });

    expect(await fornitoreScelto(page, "product-standard")).toBe("larice");
    // The quantity must not have reverted to "valore dal gestionale": that
    // mislabeling is exactly what let the reset-default-quantities action
    // wipe out a manually typed value.
    await expect(page.locator("[data-product-quantity='product-standard']")
      .locator("xpath=ancestor::*[contains(@class,'product-card')][1]")
      .getByText(/valore dal gestionale/i)).toHaveCount(0);
    await expect(page.locator("[data-product-quantity='product-standard']")).toHaveValue("2");
  });
});

test.describe("La riga proposta da un fornitore scartato", () => {
  /**
   * A past regression, seen on the store PC on a real product: the page
   * stated two contradictory things about the same supplier inches apart —
   * its proposed row with EAN, code and price, and, below it, "not in the
   * current price list".
   *
   * What tests in `tests/` can't prove, and this one does: that the
   * contradiction doesn't come back AFTER the response. Answering "no"
   * removes the warning and leaves the offer unavailable, which is exactly
   * the condition that would put that supplier back into the
   * contradictory message. It takes the full round trip: click, service
   * response, re-read comparison, re-rendered page.
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
