// @ts-check
import { defineConfig, devices } from "@playwright/test";

// ⚠ Una porta sua, diversa dalla 8765 predefinita: chi sviluppa tiene il
// programma aperto mentre lavora, e le prove non devono parlare con la sua run
// vera — né lui trovarsi la pagina cambiata sotto le mani.
const PORTA = 8799;

export default defineConfig({
  testDir: "./prove",
  // Nessun `retries`, ed è una scelta. Una prova che passa al secondo tentativo
  // sta dicendo che è capricciosa, e nasconderlo con un ritenta insegna a non
  // guardare i rossi. Se una diventa instabile si aggiusta o si toglie.
  retries: 0,
  // In fila, non in parallelo: il programma tiene UNA run e UNO stato sul disco,
  // quindi due prove insieme si pestano i piedi per costruzione.
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORTA}`,
    // Traccia e immagine solo quando è andata storta: servono a capire un
    // rosso, e tenerle sempre riempie il disco di roba che nessuno apre.
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `python3 avvia_per_le_prove.py ${PORTA}`,
    url: `http://127.0.0.1:${PORTA}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
