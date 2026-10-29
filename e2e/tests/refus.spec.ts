import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { CONSENT_TEXT_VERSION } from "../../lib/consentText";
import { CURRENT_QUESTIONNAIRE } from "../../lib/fitnessQuestionnaire";
import { expect, test } from "../fixtures";
import { startConsole, type PiConsole } from "../harness/console";
import { callAs } from "../harness/convex";
import { convexUserId, identityOf, type AccountKey, type RunInfo } from "../harness/run";
import { rememberFromTest } from "../harness/secrets";
import { card, choose, escape } from "../ui";

/*
 * Launch refusals and rules, in the browser: each scenario shows the refusal
 * the site words (messages/fr.json, messages/en.json) and that nothing came of
 * it: no session made, or none armed, as the real simulated console says
 * through its API when it is in play.
 *
 * One passenger of client A, the minor of the run, goes through them in
 * order: no fitness nor consent, then no max heart rate, then too young, then
 * an adult whose launch expires, then whose right is withdrawn. These pages
 * show health data and the setup holds machine keys: no trace, video or
 * capture is kept, even on failure. No message carries a health value.
 */
test.describe.configure({ mode: "serial" });
test.use({ trace: "off", video: "off", screenshot: "off" });

type Lang = "fr" | "en";
const catalogs: Record<Lang, Record<string, unknown>> = {
  fr: JSON.parse(readFileSync(path.join(__dirname, "..", "..", "messages", "fr.json"), "utf8")),
  en: JSON.parse(readFileSync(path.join(__dirname, "..", "..", "messages", "en.json"), "utf8")),
};

/** The site's own sentence for `key`, its `{placeholders}` filled. */
function say(lang: Lang, key: string, values: Record<string, string | number> = {}): string {
  const text = key.split(".").reduce<unknown>((node, part) => (node as Record<string, unknown>)[part], catalogs[lang]);
  if (typeof text !== "string") throw new Error(`message absent : ${key}`);
  return text.replace(/\{(\w+)\}/g, (_, name: string) => String(values[name]));
}

type Machine = { id: string; name: string; key: string };

/** What the scenarios carry from one to the next. */
const state: {
  machineA?: Machine;
  console?: PiConsole;
  riderId?: string;
  /** The shipped programme the launches use, as the console names it. */
  programme?: { id: string; name: string; hrMax: number };
  minAge?: number;
  expiredId?: string;
  revokedId?: string;
} = {};

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`étape précédente manquante : ${what}`);
  return value;
}

const as = (run: RunInfo, account: AccountKey) => identityOf(run, account);

/** Every session of a machine, as the Anheart admin lists them. */
async function sessionsOn(run: RunInfo, machineId: string) {
  return await callAs<{ _id: string; status: string }[]>(run.backend, as(run, "admin"), "query", "sessions:listSessions", {
    machineId,
  });
}

/** A machine of this organisation, made by the harness, its manager assigned; its key kept masked. */
async function createMachine(run: RunInfo, organization: "Client A" | "Client B", manager: AccountKey): Promise<Machine> {
  const organizations = await callAs<{ _id: string; name: string }[]>(
    run.backend,
    as(run, "admin"),
    "query",
    "organizations:listOrganizations",
    {},
  );
  const organizationId = organizations.find((o) => o.name === `e2e-${run.runId} ${organization}`)?._id;
  if (!organizationId) throw new Error(`organisation ${organization} absente`);
  const name = `Machine refus ${organization.slice(-1)} ${run.runId}`;
  const made = await callAs<{ machineId: string; apiKey: string }>(run.backend, as(run, "admin"), "mutation", "machines:createMachine", {
    name,
    organizationId,
    gestionnaireIds: [await convexUserId(run, manager)],
  });
  return { id: made.machineId, name, key: rememberFromTest(made.apiKey) };
}

async function startOnline(run: RunInfo, machine: Machine): Promise<PiConsole> {
  const pi = await startConsole({ runDir: run.runDir, repoRoot: run.repoRoot, backendUrl: run.backend.url, machineApiKey: machine.key });
  await expect
    .poll(async () => (await pi.panel()).body.dashboard?.state, { message: "le lien de la console", timeout: 30_000 })
    .toBe("reachable");
  return pi;
}

/** Opens the launch dialog of a machine of "My machines". */
async function openLaunch(page: Page, lang: Lang, machineName: string) {
  await page.goto(`/${lang}/dashboard/my-machines`);
  const launch = card(page, machineName).getByRole("button", { name: say(lang, "training.launch.button") });
  await expect(launch).toBeEnabled({ timeout: 30_000 });
  await launch.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText(say(lang, "training.launch.title")).first()).toBeVisible();
  return dialog;
}

/** The manager's physiology of the rider, on the patient's page. */
async function setPhysiology(page: Page, riderId: string, hrMax: number, birthYear: number) {
  await page.goto(`/fr/dashboard/patients/${riderId}`);
  const physiology = card(page, "Physiologie");
  await physiology.getByLabel("FC max mesurée (bpm)").fill(String(hrMax));
  await physiology.getByLabel("Année de naissance").fill(String(birthYear));
  await physiology.getByRole("button", { name: "Enregistrer" }).click();
  await expect(page.getByText("Physiologie enregistrée.")).toBeVisible();
}

/** The rider launches the shipped programme from "My machines"; returns the session. */
async function launch(rider: Page, machineName: string, programme: string): Promise<string> {
  const dialog = await openLaunch(rider, "fr", machineName);
  await choose(rider, dialog.getByRole("combobox").first(), new RegExp(`^${escape(programme)} ·`));
  await dialog.getByRole("button", { name: say("fr", "training.launch.submit"), exact: true }).click();
  await expect(rider).toHaveURL(/\/fr\/dashboard\/sessions\/[^/]+\/live$/);
  await expect(rider.getByText(say("fr", "training.session.pending"))).toBeVisible();
  return rider.url().split("/").at(-2) as string;
}

test.beforeAll(async ({ run }) => {
  test.setTimeout(120_000);
  const machine = await createMachine(run, "Client A", "gestionnaireA");
  state.machineA = machine;
  state.riderId = await convexUserId(run, "passagerMineurA");
  const managerId = await convexUserId(run, "gestionnaireA");
  // Not under test here (the journey makes them on the site): the rider is
  // the manager's patient and holds the launch right on the machine.
  await callAs(run.backend, as(run, "orgAdminA"), "mutation", "users:assignGestionnaireToUser", {
    userId: state.riderId,
    gestionnaireId: managerId,
  });
  await callAs(run.backend, as(run, "gestionnaireA"), "mutation", "training:grantLaunchRight", {
    machineId: machine.id,
    userId: state.riderId,
  });
  state.console = await startOnline(run, machine);
  const standard = (await state.console.profiles()).body.profiles.find((p) => p.profile_id === "standard_30_min");
  if (!standard) throw new Error("profil livré standard_30_min absent de la console");
  state.programme = { id: "standard_30_min", name: String(standard.name), hrMax: Number(standard.subject_hr_max) };
  const medical = JSON.parse(readFileSync(path.join(run.repoRoot, "raspberry-pi", "config", "medical_parameters.json"), "utf8"));
  state.minAge = Number(medical.parameters.min_rider_age);
});

test.afterAll(async () => {
  await state.console?.stop();
});

test("3. sans aptitude ni consentement, la machine n'est pas lançable ; validés, elle le devient (ANH-143)", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(180_000);
  const machine = required(state.machineA, "machine");
  const rider = await pageAs("passagerMineurA");
  const blocked = (code: string) =>
    say("fr", "training.myMachines.myReadinessBlocked", { reason: say("fr", `errors.${code}`) });
  const launchButton = () => card(rider, machine.name).getByRole("button", { name: say("fr", "training.launch.button") });

  await rider.goto("/fr/dashboard/my-machines");
  await expect(card(rider, machine.name).getByText(blocked("rider_not_fit"))).toBeVisible({ timeout: 30_000 });
  await expect(launchButton()).toBeDisabled();

  // The passenger answers the questionnaire: still not fit until a manager validates it.
  const fitness = card(rider, "Aptitude");
  const questions = fitness.getByRole("group");
  await expect(questions.first()).toBeVisible();
  for (let index = 0; index < (await questions.count()); index += 1) {
    await questions.nth(index).getByRole("button", { name: "Non", exact: true }).click();
  }
  await fitness.getByRole("button", { name: "Envoyer la saisie" }).click();
  await expect(fitness.getByText(/à valider\./)).toBeVisible();
  await expect(card(rider, machine.name).getByText(blocked("rider_not_fit"))).toBeVisible();

  // The manager validates it on the patient's page: consent is then what is missing.
  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/patients/${required(state.riderId, "passager")}`);
  const review = card(manager, "Aptitude");
  await review.getByLabel("Valide jusqu'au").fill(new Date(Date.now() + 365 * 24 * 3600 * 1000).toISOString().slice(0, 10));
  await review.getByRole("button", { name: "Valider l'aptitude" }).click();
  await expect(review.getByText(/Apte jusqu'au/)).toBeVisible();
  await rider.reload();
  await expect(card(rider, machine.name).getByText(blocked("consent_missing"))).toBeVisible();
  await expect(launchButton()).toBeDisabled();

  // Signed: the machine can be launched.
  const consent = card(rider, "Consentement");
  await consent.getByRole("checkbox", { name: say("fr", "training.consent.confirmRead") }).check();
  await consent.getByRole("button", { name: say("fr", "training.consent.sign") }).click();
  await expect(consent.getByText(/Signé le/)).toBeVisible();
  await expect(card(rider, machine.name).getByText(/^Lancement impossible/)).toHaveCount(0);
  await expect(launchButton()).toBeEnabled({ timeout: 30_000 });

  // Withdrawn: refused again; signed again: launchable again.
  await consent.getByRole("button", { name: say("fr", "training.consent.withdrawSelf") }).click();
  const withdrawal = rider.getByRole("dialog");
  await expect(withdrawal.getByText(say("fr", "training.consent.withdrawConfirmTitle"))).toBeVisible();
  await withdrawal.getByRole("button", { name: say("fr", "training.consent.withdrawConfirm") }).click();
  await expect(rider.getByText(say("fr", "training.consent.withdrawnSuccess")).first()).toBeVisible();
  await expect(card(rider, machine.name).getByText(blocked("consent_missing"))).toBeVisible();
  await expect(launchButton()).toBeDisabled();
  await consent.getByRole("checkbox", { name: say("fr", "training.consent.confirmRead") }).check();
  await consent.getByRole("button", { name: say("fr", "training.consent.sign") }).click();
  await expect(launchButton()).toBeEnabled({ timeout: 30_000 });

  expect(await sessionsOn(run, machine.id), "aucune séance créée").toHaveLength(0);
});

test("2. sans FC max, le lancement est refusé avec sa raison", async ({ pageAs, run }) => {
  const machine = required(state.machineA, "machine");
  const programme = required(state.programme, "programme");
  const rider = await pageAs("passagerMineurA");

  await rider.goto("/fr/dashboard/my-machines");
  await expect(rider.getByText(say("fr", "training.myMachines.myHrMaxMissing"))).toBeVisible();
  const dialog = await openLaunch(rider, "fr", machine.name);
  await expect(dialog.getByText(say("fr", "training.launch.hrMaxMissing"))).toBeVisible();
  await choose(rider, dialog.getByRole("combobox").first(), new RegExp(`^${escape(programme.name)} ·`));
  await expect(dialog.getByRole("button", { name: say("fr", "training.launch.submit"), exact: true })).toBeDisabled();

  // The manager, launching for this passenger, is told the same, in English.
  const manager = await pageAs("gestionnaireA");
  const forRider = await openLaunch(manager, "en", machine.name);
  await choose(manager, forRider.getByRole("combobox").nth(1), new RegExp(escape(run.accounts.passagerMineurA.email)));
  await expect(forRider.getByText(say("en", "training.launch.hrMaxMissing"))).toBeVisible();
  await expect(forRider.getByRole("button", { name: say("en", "training.launch.submit"), exact: true })).toBeDisabled();

  expect(await sessionsOn(run, machine.id), "aucune séance créée").toHaveLength(0);
});

test("1. un passager mineur ne peut pas lancer, la raison est affichée", async ({ pageAs, run }) => {
  const machine = required(state.machineA, "machine");
  const programme = required(state.programme, "programme");
  const minAge = required(state.minAge, "âge minimum");
  const manager = await pageAs("gestionnaireA");
  // Fifteen by the site's count: under the medical file's minimum.
  await setPhysiology(manager, required(state.riderId, "passager"), programme.hrMax, new Date().getUTCFullYear() - 15);

  // The manager's dialog says it before any launch.
  const forRider = await openLaunch(manager, "fr", machine.name);
  await choose(manager, forRider.getByRole("combobox").nth(1), new RegExp(escape(run.accounts.passagerMineurA.email)));
  await expect(forRider.getByText(say("fr", "training.launch.tooYoung", { min: minAge }))).toBeVisible();
  await expect(forRider.getByRole("button", { name: say("fr", "training.launch.submit"), exact: true })).toBeDisabled();

  // The passenger's own launch is refused by the server, and stays on the page.
  const rider = await pageAs("passagerMineurA");
  const dialog = await openLaunch(rider, "fr", machine.name);
  await choose(rider, dialog.getByRole("combobox").first(), new RegExp(`^${escape(programme.name)} ·`));
  await dialog.getByRole("button", { name: say("fr", "training.launch.submit"), exact: true }).click();
  await expect(dialog.getByText(say("fr", "errors.rider_too_young"))).toBeVisible();
  await expect(rider).toHaveURL(/\/fr\/dashboard\/my-machines$/);

  expect(await sessionsOn(run, machine.id), "aucune séance créée").toHaveLength(0);
});

test("4. une séance manuelle se lance à la console seulement : visible, jamais proposée par le site", async ({
  pageAs,
  run,
}) => {
  const machine = required(state.machineA, "machine");
  const rider = await pageAs("passagerMineurA");
  const manual = say("en", "training.manualOnlyAtMachine");

  await rider.goto("/en/dashboard/my-machines");
  await expect(card(rider, machine.name).getByText(manual)).toBeVisible();
  const dialog = await openLaunch(rider, "en", machine.name);
  await expect(dialog.getByText(manual)).toBeVisible();
  // The programmes offered are the console's automatic ones, none manual.
  await dialog.getByRole("combobox").first().click();
  const offered = await rider.getByRole("option").allTextContents();
  await rider.keyboard.press("Escape");
  const shipped = (await required(state.console, "console").profiles()).body.profiles.map((p) => String(p.name));
  expect(offered.length).toBe(shipped.length);
  expect(offered.every((option) => shipped.some((name) => option.startsWith(`${name} ·`)))).toBe(true);
  expect(offered.some((option) => /manu/i.test(option))).toBe(false);

  expect(await sessionsOn(run, machine.id), "aucune séance créée").toHaveLength(0);
});

test("5. une séance en attente expire, la console revenue ne la prend pas (ANH-144)", async ({ pageAs, run }) => {
  test.setTimeout(360_000);
  const machine = required(state.machineA, "machine");
  const programme = required(state.programme, "programme");
  const minAge = required(state.minAge, "âge minimum");
  const manager = await pageAs("gestionnaireA");
  await setPhysiology(manager, required(state.riderId, "passager"), programme.hrMax, new Date().getUTCFullYear() - minAge - 20);

  // The console stops; the site still holds the machine online for 90 s.
  expect(await required(state.console, "console").stop()).toBe(0);
  state.console = undefined;
  const rider = await pageAs("passagerMineurA");
  const sessionId = await launch(rider, machine.name, programme.name);
  state.expiredId = sessionId;

  // 90 s without its machine (PENDING_SESSION_TTL_S), then the minute's sweep.
  await expect(rider.getByText(say("fr", "errors.expired_before_start"))).toBeVisible({ timeout: 240_000 });
  await expect(rider.getByText(say("fr", "training.session.failed"))).toBeVisible();
  await rider.goto(`/en/dashboard/sessions/${sessionId}/live`);
  await expect(rider.getByText(say("en", "errors.expired_before_start"))).toBeVisible();
  await expect(rider.getByText(say("en", "training.session.failed"))).toBeVisible();

  // Back, the console has nothing to arm (scenario 6 then shows it polling,
  // and offering the next launch, never this one).
  const pi = await startOnline(run, machine);
  state.console = pi;
  expect((await pi.status()).body.run_state).toBe("idle");
  expect((await pi.remoteLaunch()).body).toBeNull();
  expect((await sessionsOn(run, machine.id)).map((s) => s.status)).toEqual(["failed"]);
});

test("6. droit retiré avant le départ : la console n'arme pas, le site dit pourquoi (ANH-144)", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(240_000);
  const machine = required(state.machineA, "machine");
  const pi = required(state.console, "console");
  const expiredId = required(state.expiredId, "séance expirée");
  const rider = await pageAs("passagerMineurA");
  const sessionId = await launch(rider, machine.name, required(state.programme, "programme").name);
  state.revokedId = sessionId;

  // The operator at the console sees the launch, ready to be confirmed.
  expect((await pi.attest()).status).toBe(200);
  pi.keepPresent();
  const offered = new Set<string>();
  await expect
    .poll(
      async () => {
        const offer = (await pi.remoteLaunch()).body;
        if (offer) offered.add(offer.session_id);
        return offer?.session_id === sessionId && offer.can_confirm;
      },
      { message: "la console propose ce départ, confirmable", timeout: 30_000 },
    )
    .toBe(true);
  expect(offered.has(expiredId), "la séance expirée n'est jamais proposée").toBe(false);

  // The manager withdraws the right; the operator confirms right after.
  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/machines/${machine.id}`);
  await card(manager, say("fr", "training.rights.title")).getByRole("button", { name: say("fr", "training.rights.revoke") }).click();
  const confirm = manager.getByRole("dialog");
  await expect(confirm.getByText(say("fr", "training.rights.revokeConfirm"))).toBeVisible();
  await confirm.getByRole("button", { name: say("fr", "training.rights.revoke") }).click();
  await expect(manager.getByText(say("fr", "feedback.launchRightRevoked")).first()).toBeVisible();
  // 202 when the console still held the offer (the server then refuses the
  // start), 409 when its next poll had already withdrawn it.
  const confirmed = (await pi.confirmLaunch(sessionId)).status;
  expect([202, 409]).toContain(confirmed);
  console.log(`[refus] confirmation à la console après le retrait du droit : ${confirmed}`);

  // The site says why, while the console never leaves rest.
  const states = new Set<string>();
  await manager.goto(`/fr/dashboard/sessions/${sessionId}/live`);
  await expect
    .poll(
      async () => {
        states.add((await pi.status()).body.run_state);
        return await manager.getByText(say("fr", "errors.right_revoked_before_start")).isVisible();
      },
      { message: "le site affiche le refus", timeout: 120_000, intervals: [1_000] },
    )
    .toBe(true);
  await expect(manager.getByText(say("fr", "training.session.failed"))).toBeVisible();
  expect([...states], "la console reste au repos").toEqual(["idle"]);
  expect((await pi.snapshot()).body?.measured.output_rpm ?? 0).toBe(0);
  // The offer is gone (the poll withdrew it) or refused (the server refused the start).
  const offer = (await pi.remoteLaunch()).body;
  expect(offer === null ? "aucune offre" : offer.state).toMatch(/^(aucune offre|refused)$/);

  await manager.goto(`/en/dashboard/sessions/${sessionId}/live`);
  await expect(manager.getByText(say("en", "errors.right_revoked_before_start"))).toBeVisible();
  await rider.goto("/fr/dashboard/my-machines");
  await expect(rider.getByText(say("fr", "training.myMachines.noRights"))).toBeVisible();
  expect((await sessionsOn(run, machine.id)).map((s) => s.status)).toEqual(["failed", "failed"]);
});

test("7. deux organisations ne se voient pas ; l'admin Anheart voit les deux", async ({ pageAs, run }) => {
  test.setTimeout(240_000);
  const machineA = required(state.machineA, "machine A");
  const programme = required(state.programme, "programme");
  const riderA = run.accounts.passagerMineurA;
  const sessionA = required(state.revokedId, "séance de A");

  // Client B: a machine, its manager, a ready passenger and a launch waiting
  // there, made by the harness (not under test here).
  const machineB = await createMachine(run, "Client B", "gestionnaireB");
  const riderBId = await convexUserId(run, "passagerB");
  const managerBId = await convexUserId(run, "gestionnaireB");
  await callAs(run.backend, as(run, "admin"), "mutation", "users:assignGestionnaireToUser", {
    userId: riderBId,
    gestionnaireId: managerBId,
  });
  const answers = Object.fromEntries(CURRENT_QUESTIONNAIRE.questions.map((q) => [q.id, q.outcomes.yes === "fit"]));
  const { assessmentId } = await callAs<{ assessmentId: string }>(run.backend, as(run, "passagerB"), "mutation", "fitness:submitFitnessAnswers", {
    questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
    answers,
  });
  await callAs(run.backend, as(run, "gestionnaireB"), "mutation", "fitness:validateFitnessAssessment", {
    assessmentId,
    validUntil: Date.now() + 30 * 24 * 3600 * 1000,
  });
  await callAs(run.backend, as(run, "passagerB"), "mutation", "fitness:signConsent", { textVersion: CONSENT_TEXT_VERSION });
  await callAs(run.backend, as(run, "gestionnaireB"), "mutation", "training:setUserPhysiology", {
    userId: riderBId,
    hrMax: programme.hrMax,
    birthYear: new Date().getUTCFullYear() - required(state.minAge, "âge minimum") - 20,
  });
  await callAs(run.backend, as(run, "gestionnaireB"), "mutation", "training:grantLaunchRight", {
    machineId: machineB.id,
    userId: riderBId,
  });
  const piB = await startOnline(run, machineB);
  let sessionB: string;
  try {
    await expect
      .poll(
        async () =>
          (await callAs<{ status: string } | null>(run.backend, as(run, "admin"), "query", "machines:getMachine", {
            machineId: machineB.id,
          }))?.status,
        { message: "la machine B en ligne", timeout: 30_000 },
      )
      .toBe("online");
    await expect
      .poll(
        async () =>
          (await callAs<{ profileId: string }[]>(run.backend, as(run, "admin"), "query", "training:listMachineProfiles", {
            machineId: machineB.id,
          })).length,
        { message: "les programmes de la machine B", timeout: 30_000 },
      )
      .toBeGreaterThan(0);
    sessionB = await callAs<string>(run.backend, as(run, "passagerB"), "mutation", "training:launchAutoSession", {
      machineId: machineB.id,
      profileId: programme.id,
    });
  } finally {
    expect(await piB.stop()).toBe(0);
  }

  // One organisation's manager and passenger, against the other's machine,
  // passengers and sessions: absent from the lists (next to their own, shown),
  // refused at their own address.
  const sides = [
    {
      manager: "gestionnaireB",
      passenger: "passagerB",
      lang: "fr",
      own: { machine: machineB, rider: run.accounts.passagerB },
      // What the passenger's "My machines" shows of their own, before what it must not.
      ownPassengerView: (page: Page) => card(page, machineB.name),
      other: { machine: machineA, rider: riderA, riderId: required(state.riderId, "passager"), session: sessionA },
    },
    {
      manager: "gestionnaireA",
      passenger: "passagerMineurA",
      lang: "en",
      own: { machine: machineA, rider: riderA },
      ownPassengerView: (page: Page) => page.getByText(say("en", "training.myMachines.noRights")),
      other: { machine: machineB, rider: run.accounts.passagerB, riderId: riderBId, session: sessionB },
    },
  ] as const;
  for (const side of sides) {
    const { lang, own, other } = side;
    const manager = await pageAs(side.manager);
    await manager.goto(`/${lang}/dashboard/machines`);
    await expect(manager.getByRole("row", { name: new RegExp(escape(own.machine.name)) })).toBeVisible();
    await expect(manager.getByRole("row", { name: new RegExp(escape(other.machine.name)) })).toHaveCount(0);
    await manager.goto(`/${lang}/dashboard/patients`);
    await expect(manager.getByRole("row").filter({ hasText: own.rider.email })).toBeVisible();
    await expect(manager.getByRole("row").filter({ hasText: other.rider.email })).toHaveCount(0);
    await manager.goto(`/${lang}/dashboard/sessions`);
    await expect(manager.getByRole("row").filter({ hasText: own.machine.name }).first()).toBeVisible();
    await expect(manager.getByRole("row").filter({ hasText: other.machine.name })).toHaveCount(0);
    await manager.goto(`/${lang}/dashboard/machines/${other.machine.id}`);
    await expect(manager.getByText(say(lang, "machines.notFound"))).toBeVisible();
    await manager.goto(`/${lang}/dashboard/patients/${other.riderId}`);
    await expect(manager.getByText(say(lang, "users.noUsers"))).toBeVisible();
    await manager.goto(`/${lang}/dashboard/sessions/${other.session}`);
    await expect(manager.getByText(say(lang, "sessions.notFound"))).toBeVisible();

    const passenger = await pageAs(side.passenger);
    await passenger.goto(`/${lang}/dashboard/my-machines`);
    await expect(side.ownPassengerView(passenger)).toBeVisible();
    await expect(card(passenger, other.machine.name)).toHaveCount(0);
    await passenger.goto(`/${lang}/dashboard/sessions/${other.session}`);
    await expect(passenger.getByText(say(lang, "sessions.notFound"))).toBeVisible();
    await passenger.goto(`/${lang}/dashboard/machines/${other.machine.id}`);
    await expect(passenger.getByText(say(lang, "machines.notFound"))).toBeVisible();
  }

  // The Anheart admin sees both.
  const admin = await pageAs("admin");
  await admin.goto("/fr/dashboard/machines");
  for (const machine of [machineA, machineB]) {
    await expect(admin.getByRole("row", { name: new RegExp(escape(machine.name)) })).toBeVisible();
  }
  await admin.goto("/fr/dashboard/sessions");
  for (const machine of [machineA, machineB]) {
    await expect(admin.getByRole("row").filter({ hasText: machine.name }).first()).toBeVisible();
  }
  await admin.goto(`/fr/dashboard/sessions/${sessionB}`);
  const riderB = run.accounts.passagerB;
  await expect(admin.getByRole("heading", { name: `${riderB.firstName} ${riderB.lastName}`, exact: true })).toBeVisible();
  await admin.goto(`/fr/dashboard/sessions/${sessionA}`);
  await expect(admin.getByRole("heading", { name: `${riderA.firstName} ${riderA.lastName}`, exact: true })).toBeVisible();
});

// Nothing to test yet: the blocking incident (ANH-145) is not built.
test.skip("8. un incident bloquant interdit le lancement : bloqué par ANH-145", () => {});
