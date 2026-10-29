import { test as base } from "@playwright/test";
import { startConsole, type PiConsole } from "./harness/console";
import { readRunInfo, type AccountKey, type RunInfo } from "./harness/run";

export { expect } from "@playwright/test";

type TestFixtures = {
  /** The account the page is signed in as; none by default. */
  account: AccountKey | undefined;
  /** Starts the real Pi console with this machine key; stopped after the test. */
  startPiConsole: (machineApiKey: string) => Promise<PiConsole>;
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
        convexSiteUrl: run.backend.siteUrl,
        machineApiKey,
      });
      started.push(piConsole);
      return piConsole;
    });
    for (const piConsole of started) await piConsole.stop();
  },
});
