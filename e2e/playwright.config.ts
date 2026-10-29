import { defineConfig, devices } from "@playwright/test";
import path from "node:path";
import { outputRoot } from "./harness/output";

/**
 * End-to-end tests of the dashboard, `npm run test:e2e`.
 *
 * `global-setup.ts` mounts a disposable environment and tears it down at the
 * end, on failure and on Ctrl-C: a local Convex backend on 127.0.0.1, Clerk
 * test accounts of the development instance (keys from the file named by
 * E2E_ENV_FILE), the site built and served locally. The simulated Pi console
 * is a fixture of the tests (`fixtures.ts`). One run at a time.
 *
 * Nothing is written in the repository: the report, traces and captures go to
 * E2E_OUTPUT_DIR, by default one folder of the system's temporary folder,
 * replaced on each run.
 */
// No text snapshot of the page in a failure's error context: it would carry
// what the page showed (a passenger's health data, a key shown once).
process.env.PLAYWRIGHT_NO_COPY_PROMPT = "1";

const output = outputRoot();
const results = path.join(output, "test-results");
const report = path.join(output, "rapport");

export default defineConfig({
  testDir: "./tests",
  outputDir: results,
  globalSetup: "./global-setup.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: report }],
    ["./secret-scan.ts", { output, folders: [results, report, path.join(__dirname, "..", ".next")] }],
  ],
  use: {
    ...devices["Desktop Chrome"],
    trace: "retain-on-failure",
    video: "retain-on-failure",
    screenshot: "only-on-failure",
  },
});
