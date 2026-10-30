import type { Browser } from "@playwright/test";

type ClerkInBrowser = {
  loaded: boolean;
  client: {
    signIn: {
      create(params: { strategy: "ticket"; ticket: string }): Promise<{
        status: string;
        createdSessionId: string | null;
      }>;
    };
  };
  setActive(params: { session: string | null; organization: string }): Promise<void>;
};

/**
 * Signs one account in without a password: a sign-in token of the Backend
 * API used as a ticket by the site's own Clerk, the organisation made active,
 * then the browser state saved. The testing token is added to the Frontend
 * API calls of this step only, against the instance's bot protection. No
 * trace is recorded here.
 */
export async function signIn(
  browser: Browser,
  options: {
    baseURL: string;
    frontendApi: string;
    testingToken: string;
    ticket: string;
    organizationId: string;
    welcome: string;
    storageState: string;
  },
): Promise<void> {
  const context = await browser.newContext({ baseURL: options.baseURL });
  try {
    await context.route(`https://${options.frontendApi}/**`, (route) => {
      const url = new URL(route.request().url());
      url.searchParams.set("__clerk_testing_token", options.testingToken);
      return route.continue({ url: url.toString() });
    });
    const page = await context.newPage();
    await page.goto("/fr");
    await page.waitForFunction(
      () => (window as unknown as { Clerk?: ClerkInBrowser }).Clerk?.loaded === true,
      null,
      { timeout: 30_000 },
    );
    const outcome = await page.evaluate(
      async ({ ticket, organizationId }) => {
        const clerk = (window as unknown as { Clerk: ClerkInBrowser }).Clerk;
        const attempt = await clerk.client.signIn.create({ strategy: "ticket", ticket });
        if (attempt.status !== "complete") return attempt.status;
        await clerk.setActive({ session: attempt.createdSessionId, organization: organizationId });
        return "complete";
      },
      { ticket: options.ticket, organizationId: options.organizationId },
    );
    if (outcome !== "complete") throw new Error(`Connexion par ticket inachevée : ${outcome}`);
    await page.goto("/fr/dashboard");
    await page.getByText(options.welcome).waitFor({ timeout: 30_000 });
    await context.storageState({ path: options.storageState });
  } finally {
    await context.close();
  }
}
