import { portIsFree } from "../harness/processes";
import { expect, test } from "../fixtures";

test.describe("la console Pi simulée", () => {
  test.use({ account: "admin" });

  test("répond à son API, met la machine en ligne sur le site, puis s'arrête", async ({
    page,
    run,
    startPiConsole,
  }) => {
    const piConsole = await startPiConsole(run.machine.apiKey);

    const status = await piConsole.status();
    expect(status.status).toBe(200);
    expect(status.body.run_state).toBe("idle");
    expect((await piConsole.attest()).status).toBe(200);
    expect((await piConsole.presence()).status).toBe(200);
    await expect
      .poll(async () => (await piConsole.panel()).body.dashboard?.state, { timeout: 30_000 })
      .toBe("reachable");

    await page.goto(`/fr/dashboard/machines/${run.machine.id}`);
    await expect(page.getByText("En ligne").first()).toBeVisible({ timeout: 30_000 });

    expect(await piConsole.stop()).toBe(0);
    expect(await portIsFree(piConsole.port)).toBe(true);
  });
});
