import { test as base, type BrowserContext, type Page } from "@playwright/test";
import { startConsole, type PiConsole } from "./harness/console";
import { readRunInfo, type AccountKey, type RunInfo } from "./harness/run";

export { expect } from "@playwright/test";

type TestFixtures = {
  /** The account the page is signed in as; none by default. */
  account: AccountKey | undefined;
  /** Starts the real Pi console with this machine key; stopped after the test. */
  startPiConsole: (machineApiKey: string) => Promise<PiConsole>;
  /** A page signed in as this account, in a browser context of its own closed after the test. */
  pageAs: (account: AccountKey) => Promise<Page>;
};

export const test = base.extend<TestFixtures, { run: RunInfo }>({
  run: [async ({}, provide) => provide(readRunInfo()), { scope: "worker" }],
  account: [undefined, { option: true }],
  baseURL: async ({ run }, provide) => provide(run.baseURL),
  storageState: async ({ run, account }, provide) =>
    provide(account ? run.accounts[account].storageState : undefined),
  startPiConsole: async ({ run }, provide) => {
    const started: PiConsole[] = [];
    await provide(async (machineApiKey) => {
      const piConsole = await startConsole({
        runDir: run.runDir,
        repoRoot: run.repoRoot,
        backendUrl: run.backend.url,
        machineApiKey,
      });
      started.push(piConsole);
      return piConsole;
    });
    for (const piConsole of started) await piConsole.stop();
  },
  pageAs: async ({ browser, run }, provide) => {
    const contexts: BrowserContext[] = [];
    await provide(async (account) => {
      const context = await browser.newContext({
        baseURL: run.baseURL,
        storageState: run.accounts[account].storageState,
      });
      contexts.push(context);
      return await context.newPage();
    });
    for (const context of contexts) await context.close();
  },
});
