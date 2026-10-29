import { chromium } from "@playwright/test";
import { randomBytes } from "node:crypto";
import { rmSync } from "node:fs";
import path from "node:path";
import { clerkApi } from "./harness/clerk";
import {
  callAs,
  newWebhookSecret,
  pushFunctions,
  replayClerkEvent,
  setDeploymentEnv,
  startBackend,
} from "./harness/convex";
import {
  cleanLeftoverRuns,
  freePort,
  killRunProcessesNow,
  newRunDir,
  portIsFree,
  runToEnd,
  startProcess,
  stopRunProcesses,
  waitForHttp,
} from "./harness/processes";
import {
  ACCOUNTS,
  ORGANIZATIONS,
  identityOf,
  writeRunInfo,
  type Account,
  type AccountKey,
  type OrganizationKey,
  type RunInfo,
} from "./harness/run";
import {
  assertLocal,
  childEnv,
  loadClerkKeys,
  redact,
  refuseCloudConvex,
  remember,
} from "./harness/secrets";
import { signIn } from "./harness/signin";

const repoRoot = path.resolve(__dirname, "..");

function say(message: string): void {
  console.log(`[e2e] ${redact(message)}`);
}

function seconds(since: number): string {
  return `${((Date.now() - since) / 1000).toFixed(1)} s`;
}

/**
 * Mounts the disposable environment, and returns its teardown (run by
 * Playwright at the end, on failure and on Ctrl-C). A failure here tears down
 * what was already mounted before it is reported.
 */
export default async function globalSetup(): Promise<() => Promise<void>> {
  const started = Date.now();
  refuseCloudConvex();
  const keys = loadClerkKeys();
  const clerk = clerkApi(keys);

  const leftRuns = await cleanLeftoverRuns();
  const leftClerk = await clerk.deleteTestResources();
  say(
    `restes d'exécutions précédentes : ${leftRuns} dossier(s), ${leftClerk.users} compte(s) et ${leftClerk.organizations} organisation(s) Clerk supprimés`,
  );
  await clerk.checkInstance(Object.values(ACCOUNTS).map((account) => account.role));

  const runId = randomBytes(4).toString("hex");
  const runDir = newRunDir(runId);
  process.env.E2E_RUN_DIR = runDir;
  const lastResort = () => killRunProcessesNow(runDir);
  process.once("exit", lastResort);
  const ports: number[] = [];

  let tornDown = false;
  const teardown = async () => {
    if (tornDown) return;
    tornDown = true;
    const since = Date.now();
    const problems: string[] = [];
    await stopRunProcesses(runDir).catch((error: unknown) => problems.push(`processus : ${String(error)}`));
    let left = { users: -1, organizations: -1 };
    try {
      await clerk.deleteTestResources();
      left = await clerk.countTestResources();
    } catch (error) {
      problems.push(`Clerk : ${String(error)}`);
    }
    const busy = [];
    for (const port of ports) if (!(await portIsFree(port))) busy.push(port);
    rmSync(runDir, { recursive: true, force: true });
    process.removeListener("exit", lastResort);
    say(
      `démontage en ${seconds(since)} : ports ${busy.length === 0 ? "libres" : `encore pris ${busy.join(", ")}`} (${ports.length}), ` +
        `Clerk au préfixe e2e- : ${left.users} compte(s), ${left.organizations} organisation(s), dossier de l'exécution supprimé`,
    );
    if (left.users !== 0 || left.organizations !== 0 || busy.length > 0) {
      problems.push("le démontage a laissé des restes");
    }
    if (problems.length > 0) throw new Error(redact(problems.join("\n")));
  };

  try {
    say(`exécution ${runId}, dossier ${runDir}`);

    let step = Date.now();
    const backend = await startBackend(runDir, runId);
    ports.push(Number(new URL(backend.url).port), Number(new URL(backend.siteUrl).port));
    say(`backend Convex local prêt en ${seconds(step)} sur ${backend.url}`);

    const sitePort = await freePort();
    ports.push(sitePort);
    // `localhost`, not 127.0.0.1: bound to 127.0.0.1, `next start` takes the
    // rewrites of the language middleware (made on localhost) for another
    // host and proxies them to itself without end.
    const baseURL = assertLocal(`http://localhost:${sitePort}`);
    const siteEnv = childEnv({
      NEXT_PUBLIC_CONVEX_URL: backend.url,
      NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY: keys.publishableKey,
      CLERK_SECRET_KEY: keys.secretKey,
      NEXT_TELEMETRY_DISABLED: "1",
    });
    const nextBin = path.join(repoRoot, "node_modules", ".bin", "next");
    const buildStarted = Date.now();
    const build = runToEnd(runDir, "next-build", nextBin, ["build"], { cwd: repoRoot, env: siteEnv }).then(() =>
      say(`site construit en ${seconds(buildStarted)}`),
    );
    // Awaited below; a failure of another step first stops it in the teardown.
    build.catch(() => undefined);

    // Clerk organisations first: the deployment needs the Anheart one's id.
    step = Date.now();
    const organizations = {} as Record<OrganizationKey, Record<string, unknown> & { id: string }>;
    for (const [key, label] of Object.entries(ORGANIZATIONS) as [OrganizationKey, string][]) {
      organizations[key] = await clerk.createOrganization(`e2e-${runId} ${label}`);
    }
    const webhookSecret = newWebhookSecret();
    await setDeploymentEnv(backend, {
      CLERK_JWT_ISSUER_DOMAIN: keys.issuer,
      ANHEART_ORG_ID: organizations.anheart.id,
      CLERK_WEBHOOK_SIGNING_SECRET: webhookSecret,
      // The webhook reads the verified e-mails of a member from Clerk.
      CLERK_SECRET_KEY: keys.secretKey,
      // The lowest the code accepts: an abandoned launch expires within the run.
      PENDING_SESSION_TTL_S: "90",
    });
    await pushFunctions(runDir, repoRoot, backend);
    say(`fonctions Convex poussées en ${seconds(step)}`);

    step = Date.now();
    for (const organization of Object.values(organizations)) {
      await replayClerkEvent(backend, webhookSecret, "organization.created", organization);
    }
    const accounts = {} as Record<AccountKey, Account>;
    for (const [key, spec] of Object.entries(ACCOUNTS) as [AccountKey, (typeof ACCOUNTS)[AccountKey]][]) {
      const email = `e2e-${runId}-${key.toLowerCase()}+clerk_test@example.com`;
      const user = await clerk.createUser(email, spec.firstName, spec.lastName);
      const organizationId = organizations[spec.organization].id;
      const membership = await clerk.addMember(organizationId, user.id, spec.role);
      await replayClerkEvent(backend, webhookSecret, "organizationMembership.created", membership);
      accounts[key] = {
        ...spec,
        userId: user.id,
        email,
        organizationId,
        storageState: path.join(runDir, `state-${key}.json`),
      };
    }
    say(
      `Clerk : ${Object.keys(organizations).length} organisations et ${Object.keys(accounts).length} comptes créés et reflétés dans Convex en ${seconds(step)}`,
    );

    const info: RunInfo = {
      runId,
      runDir,
      repoRoot,
      baseURL,
      issuer: keys.issuer,
      backend,
      organizations: Object.fromEntries(
        Object.entries(organizations).map(([key, value]) => [key, value.id]),
      ) as Record<OrganizationKey, string>,
      accounts,
      machine: { id: "", apiKey: "" },
    };
    const machine = await callAs<{ machineId: string; apiKey: string }>(
      backend,
      identityOf(info, "admin"),
      "mutation",
      "machines:createMachine",
      { name: `Machine E2E ${runId}` },
    );
    info.machine = { id: machine.machineId, apiKey: remember(machine.apiKey) };

    await build;
    step = Date.now();
    const server = startProcess(
      runDir,
      "next-start",
      nextBin,
      ["start", "--hostname", "localhost", "--port", String(sitePort)],
      { cwd: repoRoot, env: siteEnv },
    );
    await waitForHttp(`${baseURL}/fr`, { timeoutMs: 60_000, child: server, runDir, name: "next-start" });
    say(`site servi en ${seconds(step)} sur ${baseURL}`);

    step = Date.now();
    const testingToken = await clerk.testingToken();
    const browser = await chromium.launch();
    try {
      await Promise.all(
        (Object.keys(accounts) as AccountKey[]).map(async (key) => {
          const account = accounts[key];
          await signIn(browser, {
            baseURL,
            frontendApi: keys.frontendApi,
            testingToken,
            ticket: await clerk.signInToken(account.userId),
            organizationId: account.organizationId,
            welcome: `Bienvenue, ${account.firstName}`,
            storageState: account.storageState,
          });
        }),
      );
    } finally {
      await browser.close();
    }
    say(`${Object.keys(accounts).length} comptes connectés en ${seconds(step)}`);

    writeRunInfo(info);
    say(`environnement prêt en ${seconds(started)}`);
    return teardown;
  } catch (error) {
    await teardown().catch((failure: unknown) => say(`démontage après échec : ${String(failure)}`));
    if (!(error instanceof Error)) throw new Error(redact(String(error)));
    error.message = redact(error.message);
    error.stack = redact(error.stack ?? "");
    throw error;
  }
}
