// @ts-check
import { defineConfig, devices } from "@playwright/test";

// Its own port, different from the default 8765: developers keep the
// program open while they work, and tests must not talk to that real run,
// or have the page change under them.
const PORTA = 8799;

export default defineConfig({
  testDir: "./prove",
  // No `retries`, deliberately. A test that only passes on a second attempt
  // is flaky, and retrying silently trains people to stop looking at
  // failures. A flaky test gets fixed or removed instead.
  retries: 0,
  // Serial, not parallel: the program keeps ONE run and ONE state on disk,
  // so two tests running together would step on each other by construction.
  workers: 1,
  fullyParallel: false,
  reporter: [["list"]],
  timeout: 30_000,
  expect: { timeout: 7_000 },
  use: {
    baseURL: `http://127.0.0.1:${PORTA}`,
    // Trace and screenshot only on failure: they're for diagnosing a
    // failing test, and keeping them always would fill the disk with data
    // nobody opens.
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
