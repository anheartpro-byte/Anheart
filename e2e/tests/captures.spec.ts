import type { Browser, Page } from "@playwright/test";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { expect, test } from "../fixtures";
import type { PiConsole } from "../harness/console";
import { callAs } from "../harness/convex";
import { createMachine, startOnline, type Machine } from "../harness/machines";
import { outputRoot } from "../harness/output";
import { medicalOf, shortProfile } from "../harness/profiles";
import { prepareRider } from "../harness/riders";
import { convexUserId, identityOf, type AccountKey, type RunInfo } from "../harness/run";
import { card, choose, escape, say, type Lang } from "../ui";

/*
 * Screenshots of the main pages, in French and in English, light and dark
 * (the site follows the system's theme: the browser's colour scheme chooses
 * it), on every run, into <E2E_OUTPUT_DIR>/captures, one stable name per
 * page, language and theme. A first set is kept in docs/guides/captures/.
 *
 * Test data only: client B of the run, its fictitious manager and passenger,
 * two machines of stable names, the simulated console (simulated heart rate,
 * no real person). The prefixes of the machine keys are masked. This file
 * runs first (Playwright runs the files without traces together, by name,
 * then the others, and this is the first of them) and touches only client
 * B, which the files after it prepare again as they need it.
 */
test.describe.configure({ mode: "serial" });
test.use({ trace: "off", video: "off", screenshot: "off" });

const LANGS: Lang[] = ["fr", "en"];
const THEMES = ["clair", "sombre"] as const;
const PROGRAMME = "standard_30_min";
const folder = path.join(outputRoot(), "captures");

const state: {
  machine?: Machine;
  quiet?: Machine;
  console?: PiConsole;
  riderId?: string;
  programmeName?: string;
  shortProfileId?: string;
  sessionId?: string;
} = {};

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`étape précédente manquante : ${what}`);
  return value;
}

/** `{placeholder}` of a message as a pattern of digits, for a figure the page computes. */
const withFigure = (lang: Lang, key: string, name: string) =>
  new RegExp(escape(say(lang, key, { [name]: "\u0000" })).replace("\u0000", "\\d+"));

/**
 * One page in both languages and both themes: opened as this account, made
 * ready (`ready` waits for what the page must show), then photographed.
 */
async function shoot(
  browser: Browser,
  run: RunInfo,
  options: {
    name: string;
    account: AccountKey;
    path: string;
    ready: (page: Page, lang: Lang) => Promise<void>;
    /**
     * The whole page, for one whose cards go below the window. The dashboard
     * scrolls inside its own frame, not the document: the window is made as
     * tall as that content instead (at most 6000 px).
     */
    tall?: boolean;
  },
): Promise<void> {
  for (const lang of LANGS) {
    for (const theme of THEMES) {
      const context = await browser.newContext({
        baseURL: run.baseURL,
        storageState: run.accounts[options.account].storageState,
        colorScheme: theme === "sombre" ? "dark" : "light",
        viewport: { width: 1280, height: 900 },
        // The dates the browser itself writes, and one clock for every run.
        locale: lang === "fr" ? "fr-FR" : "en-US",
        timezoneId: "Europe/Paris",
      });
      try {
        const page = await context.newPage();
        await page.goto(`/${lang}${options.path}`);
        await options.ready(page, lang);
        if (options.tall) {
          const height = await page.evaluate(() =>
            Math.max(...Array.from(document.querySelectorAll("*"), (element) => element.scrollHeight)),
          );
          await page.setViewportSize({ width: 1280, height: Math.min(height + 64, 6000) });
          await options.ready(page, lang);
        }
        await expect(page.locator("html")).toHaveClass(new RegExp(`\\b${theme === "sombre" ? "dark" : "light"}\\b`));
        await page.screenshot({
          path: path.join(folder, `${options.name}-${lang}-${theme}.png`),
          animations: "disabled",
          caret: "hide",
          // The prefix of a machine key is not the key, but no key part is shown.
          mask: [page.locator('[data-slot="card"]').filter({ hasText: say(lang, "machineKeys.title") }).locator("code")],
        });
      } finally {
        await context.close();
      }
    }
  }
}

test.beforeAll(async ({ run }) => {
  test.setTimeout(150_000);
  rmSync(folder, { recursive: true, force: true });
  mkdirSync(folder, { recursive: true });
  const manager = await convexUserId(run, "gestionnaireB");
  state.machine = await createMachine(run, "Client B", "Machine démo", [manager]);
  state.quiet = await createMachine(run, "Client B", "Machine démo 2", [manager]);
  // The second one goes quiet now: its "machine offline" alert comes while
  // the other pages are photographed (90 s without a heartbeat, then the
  // server's task of every minute).
  expect(await (await startOnline(run, state.quiet)).stop()).toBe(0);
  state.console = await startOnline(run, state.machine);
  const standard = (await state.console.profiles()).body.profiles.find((p) => p.profile_id === PROGRAMME);
  if (!standard) throw new Error(`profil livré ${PROGRAMME} absent de la console`);
  state.programmeName = String(standard.name);
  const medical = medicalOf(run);
  // The session photographed runs the short programme of the journey: the
  // shipped one rests 3 minutes before the arm moves.
  const listed = await state.console.profiles();
  const short = shortProfile(standard, medical);
  expect((await state.console.putProfile(short, listed.body.rev)).status).toBe(200);
  state.shortProfileId = short.profile_id;
  const machineId = state.machine.id;
  await expect
    .poll(
      async () =>
        (await callAs<{ profileId: string }[]>(run.backend, identityOf(run, "admin"), "query", "training:listMachineProfiles", { machineId })).some(
          (p) => p.profileId === short.profile_id,
        ),
      { message: "le programme court sur le serveur", timeout: 60_000 },
    )
    .toBe(true);
  state.riderId = await prepareRider(run, {
    account: "passagerB",
    manager: "gestionnaireB",
    assigner: "admin",
    machineIds: [state.machine.id],
    hrMax: Number(standard.subject_hr_max),
    birthYear: new Date().getUTCFullYear() - medical.min_rider_age - 20,
  });
});

test.afterAll(async () => {
  await state.console?.stop();
});

test("captures des pages au repos : tableau de bord, Mes machines, fiche machine, patient, fenêtre de lancement", async ({
  browser,
  run,
}) => {
  test.setTimeout(240_000);
  const machine = required(state.machine, "machine");
  await shoot(browser, run, {
    name: "01-tableau-de-bord",
    account: "gestionnaireB",
    path: "/dashboard",
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: say(lang, "dashboard.title") })).toBeVisible();
      await expect(page.getByText(withFigure(lang, "dashboard.machinesTotal", "total"))).toBeVisible({ timeout: 30_000 });
    },
  });
  await shoot(browser, run, {
    name: "02-mes-machines",
    account: "passagerB",
    path: "/dashboard/my-machines",
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: say(lang, "training.myMachines.title") })).toBeVisible();
      await expect(card(page, machine.name).getByRole("button", { name: say(lang, "training.launch.button") })).toBeEnabled({
        timeout: 30_000,
      });
      await expect(card(page, say(lang, "training.consent.title"))).toBeVisible();
    },
  });
  await shoot(browser, run, {
    name: "03-fiche-machine",
    account: "admin",
    path: `/dashboard/machines/${machine.id}`,
    // Its live state and its keys are far below the top of the page.
    tall: true,
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: machine.name })).toBeVisible();
      await expect(card(page, say(lang, "training.live.title")).getByText(say(lang, "training.live.fresh"), { exact: true })).toBeVisible({
        timeout: 30_000,
      });
      await expect(card(page, say(lang, "machineKeys.title")).locator("code").first()).toBeVisible();
    },
  });
  await shoot(browser, run, {
    name: "04-patient",
    account: "gestionnaireB",
    path: `/dashboard/patients/${required(state.riderId, "passager")}`,
    ready: async (page, lang) => {
      await expect(card(page, say(lang, "training.fitness.title"))).toBeVisible();
      await expect(card(page, say(lang, "training.consent.title")).getByText(lang === "fr" ? /Signé le/ : /Signed on/)).toBeVisible({
        timeout: 30_000,
      });
    },
  });
  await shoot(browser, run, {
    name: "05-fenetre-de-lancement",
    account: "passagerB",
    path: "/dashboard/my-machines",
    ready: async (page, lang) => {
      const launch = card(page, machine.name).getByRole("button", { name: say(lang, "training.launch.button") });
      await expect(launch).toBeEnabled({ timeout: 30_000 });
      await launch.click();
      const dialog = page.getByRole("dialog");
      await choose(page, dialog.getByRole("combobox").first(), new RegExp(`^${escape(required(state.programmeName, "programme"))} ·`));
      await expect(dialog.getByRole("button", { name: say(lang, "training.launch.submit"), exact: true })).toBeEnabled();
    },
  });
});

test("captures d'une séance : en direct avec ses courbes, l'audit, puis son rapport et les alertes", async ({ browser, run }) => {
  test.setTimeout(420_000);
  const machine = required(state.machine, "machine");
  const pi = required(state.console, "console");
  const sessionId = await callAs<string>(run.backend, identityOf(run, "passagerB"), "mutation", "training:launchAutoSession", {
    machineId: machine.id,
    profileId: required(state.shortProfileId, "programme court"),
  });
  state.sessionId = sessionId;
  expect((await pi.attest()).status).toBe(200);
  pi.keepPresent();
  await expect
    .poll(async () => {
      const offer = (await pi.remoteLaunch()).body;
      return offer?.session_id === sessionId && offer.can_confirm;
    }, { message: "la console propose ce départ, confirmable", timeout: 60_000 })
    .toBe(true);
  expect((await pi.confirmLaunch(sessionId)).status).toBe(202);
  // Curves worth looking at: the whole warm-up, until the programme holds (70 s in).
  await expect
    .poll(async () => (await pi.snapshot()).body?.phase, { message: "la séance en maintien", timeout: 150_000, intervals: [1_000] })
    .toBe("hold");

  await shoot(browser, run, {
    name: "06-seance-en-direct",
    account: "passagerB",
    path: `/dashboard/sessions/${sessionId}/live`,
    ready: async (page, lang) => {
      await expect.poll(() => page.getByText(say(lang, "training.live.outputRpm"), { exact: true }).count()).toBeGreaterThanOrEqual(2);
      await expect(page.getByTestId("live-arm-speed")).toHaveText(/^\d+\.\d$/, { timeout: 30_000 });
    },
  });

  // Stopped at the console; its recovery runs while the journal is photographed.
  expect((await pi.stopSession()).status).toBe(202);
  await shoot(browser, run, {
    name: "07-audit",
    account: "admin",
    path: "/dashboard/audit",
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: say(lang, "audit.title") })).toBeVisible();
      await expect(page.locator("tbody tr").first()).toBeVisible({ timeout: 30_000 });
    },
  });

  await expect
    .poll(
      async () =>
        (await callAs<{ status: string } | null>(run.backend, identityOf(run, "admin"), "query", "training:getTrainingSession", { sessionId }))
          ?.status,
      { message: "la séance terminée", timeout: 180_000, intervals: [2_000] },
    )
    .toBe("completed");
  await shoot(browser, run, {
    name: "08-rapport-de-seance",
    account: "gestionnaireB",
    path: `/dashboard/sessions/${sessionId}/report`,
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: say(lang, "sessionReport.reportTitle") })).toBeVisible({ timeout: 30_000 });
      await expect(page.getByText(machine.name).first()).toBeVisible();
    },
  });

  const quiet = required(state.quiet, "machine silencieuse");
  await shoot(browser, run, {
    name: "09-alertes",
    account: "gestionnaireB",
    path: "/dashboard/notifications",
    ready: async (page, lang) => {
      await expect(page.getByRole("heading", { name: say(lang, "notifications.title") })).toBeVisible();
      await expect(page.locator('li[data-alert="machine_offline"]').filter({ hasText: quiet.name })).toBeVisible({ timeout: 240_000 });
    },
  });
});
