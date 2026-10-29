import type { Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { AUDIT_CSV_COLUMNS } from "../../lib/auditCsv";
import { expect, test } from "../fixtures";
import type { PiConsole } from "../harness/console";
import { callAs } from "../harness/convex";
import { createMachine, sessionsOn, startOnline, type Machine } from "../harness/machines";
import { addMember, createAccountAt, memberEmail, signInMember, type Member } from "../harness/members";
import { prepareRider } from "../harness/riders";
import { convexUserId, identityOf, type AccountKey, type RunInfo } from "../harness/run";
import { card, choose, escape, say } from "../ui";

/*
 * One named scenario per sub-ticket of ANH-122 (ANH-154 to ANH-160) and the
 * Audit page of ANH-167, in the browser, with the real simulated console:
 * each shows its visible effect and that nothing unwanted came of it.
 *
 * Runs after the other files (it changes riders and assignments they expect
 * fresh). What is not under test is prepared by the harness through the
 * Convex functions the site calls: machines, a second manager of client A,
 * riders ready to launch. These pages show health data and the setup holds
 * machine keys: no trace, video or capture is kept, even on failure, and the
 * error context of a failure is deleted (secret-scan.ts): only the error
 * message and its stack stay.
 */
test.describe.configure({ mode: "serial" });
test.use({ trace: "off", video: "off", screenshot: "off" });

const PROGRAMME = "standard_30_min";

/** What the scenarios carry from one to the next. */
const state: {
  /** M1: client A, managed by the gestionnaire of the run and the second one. */
  m1?: Machine;
  /** M2: client A, managed by the second manager only. */
  m2?: Machine;
  /** M3: client B. */
  m3?: Machine;
  manager2?: Member;
  consoles: Map<string, PiConsole>;
  sessions: { a1?: string; a2?: string; b?: string };
} = { consoles: new Map(), sessions: {} };

const MANAGER2 = { firstName: "Hugo", lastName: "Arnoux" };

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`étape précédente manquante : ${what}`);
  return value;
}

const as = (run: RunInfo, account: AccountKey) => identityOf(run, account);
const nameOf = (run: RunInfo, account: AccountKey) => `${run.accounts[account].firstName} ${run.accounts[account].lastName}`;

async function online(run: RunInfo, machine: Machine): Promise<PiConsole> {
  const pi = await startOnline(run, machine);
  state.consoles.set(machine.id, pi);
  return pi;
}

async function offline(machine: Machine): Promise<void> {
  const pi = state.consoles.get(machine.id);
  state.consoles.delete(machine.id);
  if (pi) expect(await pi.stop()).toBe(0);
}

/** A launch the rider makes from the site's own function (the site's path is played in parcours.spec.ts). */
async function launchAs(run: RunInfo, rider: AccountKey, machine: Machine): Promise<string> {
  return await callAs<string>(run.backend, as(run, rider), "mutation", "training:launchAutoSession", {
    machineId: machine.id,
    profileId: PROGRAMME,
  });
}

/** Rows of the session list that name this machine. */
const sessionRows = (page: Page, machine: Machine) => page.getByRole("row").filter({ hasText: machine.name });

test.beforeAll(async ({ run }) => {
  test.setTimeout(180_000);
  // A second manager of client A, a Clerk account added for this file.
  const manager2 = await addMember(run, { name: "gestionnaire2a", ...MANAGER2, organization: "clientA", role: "org:gestionnaire" });
  state.manager2 = manager2;
  const managerA = await convexUserId(run, "gestionnaireA");
  state.m1 = await createMachine(run, "Client A", `Machine tickets 1 ${run.runId}`, [managerA, manager2.convexId]);
  state.m2 = await createMachine(run, "Client A", `Machine tickets 2 ${run.runId}`, [manager2.convexId]);
  state.m3 = await createMachine(run, "Client B", `Machine tickets 3 ${run.runId}`, [await convexUserId(run, "gestionnaireB")]);

  const pi = await online(run, state.m1);
  const standard = (await pi.profiles()).body.profiles.find((p) => p.profile_id === PROGRAMME);
  if (!standard) throw new Error(`profil livré ${PROGRAMME} absent de la console`);
  const hrMax = Number(standard.subject_hr_max);
  const medical = JSON.parse(readFileSync(path.join(run.repoRoot, "raspberry-pi", "config", "medical_parameters.json"), "utf8"));
  const birthYear = new Date().getUTCFullYear() - Number(medical.parameters.min_rider_age) - 20;
  await prepareRider(run, { account: "passagerA", manager: "gestionnaireA", assigner: "orgAdminA", machineIds: [state.m1.id], hrMax, birthYear });
  await prepareRider(run, {
    account: "passagerMineurA",
    manager: "gestionnaireA",
    assigner: "orgAdminA",
    granter: "orgAdminA",
    machineIds: [state.m2.id],
    hrMax,
    birthYear,
  });
  await prepareRider(run, { account: "passagerB", manager: "gestionnaireB", assigner: "admin", machineIds: [state.m3.id], hrMax, birthYear });
});

test.afterAll(async () => {
  for (const pi of state.consoles.values()) await pi.stop();
});

test("ANH-154 cocher et décocher une machine sur la fiche gestionnaire, l'autre gestionnaire reste sur la machine", async ({
  pageAs,
  run,
}) => {
  const m1 = required(state.m1, "M1");
  const m2 = required(state.m2, "M2");
  const manager2 = required(state.manager2, "second gestionnaire");
  const admin = await pageAs("orgAdminA");
  const machinesCard = () => card(admin, /^Machines$/);
  const managersOfM1 = async () => {
    await admin.goto(`/fr/dashboard/machines/${m1.id}`);
    return card(admin, say("fr", "machines.gestionnaires"));
  };

  await admin.goto(`/fr/dashboard/gestionnaires/${manager2.convexId}`);
  await expect(machinesCard().getByText(m1.name)).toBeVisible();
  await expect(machinesCard().getByText(m2.name)).toBeVisible();

  // Unticked: M1 leaves this manager's sheet, the other manager stays on M1.
  await machinesCard().getByRole("button", { name: say("fr", "common.edit") }).click();
  const dialog = admin.getByRole("dialog");
  await expect(dialog.getByText(say("fr", "gestionnaires.assignMachines"))).toBeVisible();
  await dialog.getByRole("checkbox", { name: new RegExp(escape(m1.name)) }).uncheck();
  await dialog.getByRole("button", { name: say("fr", "common.save") }).click();
  await expect(admin.getByText(/Machines enregistrées\s*:\s*aucune ajoutée, 1 retirée\./)).toBeVisible();
  await expect(machinesCard().getByText(m1.name)).toHaveCount(0);
  await expect(machinesCard().getByText(m2.name)).toBeVisible();
  let managers = await managersOfM1();
  await expect(managers.getByText(nameOf(run, "gestionnaireA"))).toBeVisible();
  await expect(managers.getByText(`${MANAGER2.firstName} ${MANAGER2.lastName}`)).toHaveCount(0);

  // Ticked again: back on both sheets.
  await admin.goto(`/fr/dashboard/gestionnaires/${manager2.convexId}`);
  await machinesCard().getByRole("button", { name: say("fr", "common.edit") }).click();
  await admin.getByRole("dialog").getByRole("checkbox", { name: new RegExp(escape(m1.name)) }).check();
  await admin.getByRole("dialog").getByRole("button", { name: say("fr", "common.save") }).click();
  await expect(admin.getByText(/Machines enregistrées\s*:\s*1 ajoutée, aucune retirée\./)).toBeVisible();
  await expect(machinesCard().getByText(m1.name)).toBeVisible();
  managers = await managersOfM1();
  await expect(managers.getByText(nameOf(run, "gestionnaireA"))).toBeVisible();
  await expect(managers.getByText(`${MANAGER2.firstName} ${MANAGER2.lastName}`)).toBeVisible();
});

test("ANH-155 un gestionnaire modifie le lieu, la fiche affiche le nouveau lieu", async ({ pageAs, run }) => {
  const m1 = required(state.m1, "M1");
  const manager = await pageAs("gestionnaireA");
  const place = `Salle E2E ${run.runId}`;

  await manager.goto(`/fr/dashboard/machines/${m1.id}`);
  await card(manager, say("fr", "machines.config")).getByRole("button", { name: say("fr", "common.edit") }).click();
  const form = manager.getByRole("dialog");
  await form.getByLabel(say("fr", "machines.location")).fill(place);
  await form.getByRole("button", { name: say("fr", "common.save") }).click();
  await expect(manager.getByText(say("fr", "machines.updateSuccess"))).toBeVisible();
  await expect(form).toBeHidden();
  await expect(manager.getByText(place, { exact: true })).toBeVisible();
  // Nothing else moved: the name, both managers, no partial save.
  await expect(manager.getByRole("heading", { name: m1.name })).toBeVisible();
  await expect(manager.getByText(/Le nom et le lieu sont enregistrés, mais pas/)).toHaveCount(0);
  const managers = card(manager, say("fr", "machines.gestionnaires"));
  await expect(managers.getByText(nameOf(run, "gestionnaireA"))).toBeVisible();
  await expect(managers.getByText(`${MANAGER2.firstName} ${MANAGER2.lastName}`)).toBeVisible();
});

test("ANH-160 console coupée : badge En direct, puis données périmées après 90 s sans signal, puis En direct au retour", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(150_000);
  const m1 = required(state.m1, "M1");
  const manager = await pageAs("gestionnaireA");
  // The page ages the server's dates with its own clock: 90 s of it are skipped.
  await manager.context().clock.install();
  await manager.goto(`/fr/dashboard/machines/${m1.id}`);
  const live = card(manager, say("fr", "training.live.title"));
  const fresh = live.getByText(say("fr", "training.live.fresh"), { exact: true });
  await expect(fresh).toBeVisible({ timeout: 30_000 });
  await expect(live.getByText(say("fr", "training.live.staleDesc"))).toHaveCount(0);

  await offline(m1);
  await manager.clock.fastForward("01:35");
  await expect(live.getByText(say("fr", "training.live.stale"), { exact: true })).toBeVisible();
  await expect(live.getByText(say("fr", "training.live.staleDesc"))).toBeVisible();
  await expect(fresh).toHaveCount(0);

  // The console back: its next heartbeat makes it live again.
  await online(run, m1);
  await expect(fresh).toBeVisible({ timeout: 60_000 });
  await expect(live.getByText(say("fr", "training.live.staleDesc"))).toHaveCount(0);
});

test("ANH-159 les compteurs du tableau de bord suivent l'organisation, la console en ligne et la séance en attente", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(150_000);
  const m2 = required(state.m2, "M2");
  const admin = await pageAs("orgAdminA");
  await admin.goto("/fr/dashboard");
  const machinesCard = card(admin, say("fr", "dashboard.onlineMachines"));
  const sessionsCard = card(admin, say("fr", "dashboard.activeSessions"));

  // Client A's members, as the run made them: one admin, two managers, two passengers.
  const members = card(admin, say("fr", "nav.users"));
  await expect(members.getByText("5", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(members.getByText("1 administrateur · 2 gestionnaires · 2 patients")).toBeVisible();

  /** What the machine and session lists of the same organisation say, at this instant. */
  const expected = async () => {
    const machines = await callAs<{ _id: string; status: string; lastHeartbeat: number; isDeleted?: boolean }[]>(
      run.backend,
      as(run, "orgAdminA"),
      "query",
      "machines:listMachines",
      {},
    );
    const listed = machines.filter((m) => !m.isDeleted);
    const now = Date.now();
    const connected = listed.filter((m) => m.status !== "offline" && now - m.lastHeartbeat < 90_000);
    const count = async (status: string) =>
      (await callAs<unknown[]>(run.backend, as(run, "orgAdminA"), "query", "sessions:listSessions", { status })).length;
    return {
      total: listed.length,
      connected: connected.length,
      m2Connected: connected.some((m) => m._id === m2.id),
      active: await count("active"),
      pending: await count("pending"),
    };
  };
  /** The counters show what the lists say (both may move while a machine of another file goes quiet). */
  const shown = async () => {
    const want = await expected();
    const pendingText = want.pending === 0 ? "Aucune en attente" : `${want.pending} en attente`;
    const ok =
      (await machinesCard.getByText(String(want.connected), { exact: true }).isVisible()) &&
      (await machinesCard.getByText(say("fr", "dashboard.machinesTotal", { total: want.total })).isVisible()) &&
      (await sessionsCard.getByText(String(want.active), { exact: true }).isVisible()) &&
      (await sessionsCard.getByText(pendingText, { exact: true }).isVisible());
    return ok ? want : null;
  };
  const settled = async (message: string) => {
    let seen: Awaited<ReturnType<typeof expected>> | null = null;
    await expect
      .poll(async () => (seen = await shown()) !== null, { message, timeout: 60_000, intervals: [1_000] })
      .toBe(true);
    return seen as unknown as Awaited<ReturnType<typeof expected>>;
  };

  const before = await settled("les compteurs avant la console de M2");
  expect(before.m2Connected, "M2 hors du compte avant sa console").toBe(false);
  await online(run, m2);
  const connectedAfter = await settled("les compteurs avec M2 en ligne");
  expect(connectedAfter.m2Connected, "M2 dans le compte affiché").toBe(true);
  expect(connectedAfter.total, "aucune machine ajoutée").toBe(before.total);
  expect(
    (await callAs<{ status: string } | null>(run.backend, as(run, "orgAdminA"), "query", "machines:getMachine", { machineId: m2.id }))
      ?.status,
  ).toBe("online");

  const waiting = await launchAs(run, "passagerMineurA", m2);
  const pendingAfter = await settled("les compteurs avec une séance en attente");
  expect(pendingAfter.pending, "une séance en attente de plus").toBe(connectedAfter.pending + 1);
  expect(pendingAfter.active, "aucune séance active de plus").toBe(connectedAfter.active);
  expect(pendingAfter.total).toBe(before.total);
  // Cancelled by the passenger (one launch at a time): ANH-158 makes its own
  // right before it reads them, nothing of this test's duration in between.
  await callAs(run.backend, as(run, "passagerMineurA"), "mutation", "training:requestStop", { sessionId: waiting });
  const cancelled = await settled("les compteurs après l'annulation");
  expect(cancelled.pending, "la séance annulée n'est plus en attente").toBe(pendingAfter.pending - 1);
});

test("ANH-158 chaque rôle voit ses séances : passager, gestionnaire, admin d'organisation, admin Anheart, onglet En attente", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(150_000);
  const m1 = required(state.m1, "M1");
  const m2 = required(state.m2, "M2");
  const m3 = required(state.m3, "M3");
  await online(run, m3);
  try {
    state.sessions.b = await launchAs(run, "passagerB", m3);
  } finally {
    await offline(m3);
  }
  // Client A's two launches last, right before the "Pending" tab reads them: a
  // launch waits 90 s at most (PENDING_SESSION_TTL_S), nothing slow in between.
  state.sessions.a1 = await launchAs(run, "passagerA", m1);
  state.sessions.a2 = await launchAs(run, "passagerMineurA", m2);

  // The "Pending" tab first, while both launches of client A still wait.
  const admin = await pageAs("orgAdminA");
  await admin.goto("/fr/dashboard/sessions");
  await admin.getByRole("tab", { name: say("fr", "sessions.pending") }).click();
  await expect(sessionRows(admin, m1)).toHaveCount(1);
  await expect(sessionRows(admin, m2)).toHaveCount(1);
  const pendingTab = admin.getByRole("row").filter({ hasText: /Terminée|Échouée|Active|Interrompue/ });
  await expect(pendingTab).toHaveCount(0);

  // Each role, its sessions and not the others': a1 on M1, the launch ANH-159
  // cancelled and a2 on M2, b on M3.
  const rows = new Map([
    [m1.id, 1],
    [m2.id, 2],
    [m3.id, 1],
  ]);
  const views: { account: AccountKey; sees: Machine[]; not: Machine[] }[] = [
    { account: "passagerA", sees: [m1], not: [m2, m3] },
    { account: "gestionnaireA", sees: [m1], not: [m2, m3] },
    { account: "orgAdminA", sees: [m1, m2], not: [m3] },
    { account: "admin", sees: [m1, m2, m3], not: [] },
  ];
  for (const view of views) {
    const page = await pageAs(view.account);
    await page.goto("/fr/dashboard/sessions");
    for (const machine of view.sees) {
      await expect(sessionRows(page, machine), `${view.account} voit ${machine.name}`).toHaveCount(rows.get(machine.id) as number);
    }
    for (const machine of view.not) await expect(sessionRows(page, machine), `${view.account} ne voit pas ${machine.name}`).toHaveCount(0);
  }
  // Nothing was armed by these launches.
  for (const machine of [m1, m2, m3]) {
    expect((await sessionsOn(run, machine.id)).every((s) => s.status === "pending" || s.status === "failed")).toBe(true);
  }
});

test("ANH-156 la suppression d'une machine en séance active est refusée, la machine reste", async ({ pageAs, run }) => {
  test.setTimeout(120_000);
  const m1 = required(state.m1, "M1");
  const sessionId = required(state.sessions.a1, "séance en attente sur M1");
  const pi = required(state.consoles.get(m1.id), "console de M1");

  // The manager asks to delete the machine while it is at rest.
  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/machines/${m1.id}`);
  const deleteButton = card(manager, say("fr", "machines.dangerZone")).getByRole("button", { name: say("fr", "common.delete") });
  await expect(deleteButton).toBeEnabled();
  await deleteButton.click();
  const confirm = manager.getByRole("dialog");
  await expect(confirm.getByText(say("fr", "machines.deleteConfirm"))).toBeVisible();

  // Meanwhile the operator arms the waiting launch at the console: the session is active.
  expect((await pi.attest()).status).toBe(200);
  pi.keepPresent();
  await expect
    .poll(async () => {
      const offer = (await pi.remoteLaunch()).body;
      return offer?.session_id === sessionId && offer.can_confirm;
    }, { message: "la console propose ce départ, confirmable", timeout: 60_000 })
    .toBe(true);
  expect((await pi.confirmLaunch(sessionId)).status).toBe(202);
  await expect
    .poll(async () => (await pi.status()).body.run_state, { message: "la console tourne", timeout: 30_000 })
    .toBe("running");
  const status = async () =>
    (await callAs<{ status: string; isDeleted?: boolean } | null>(run.backend, as(run, "admin"), "query", "machines:getMachine", {
      machineId: m1.id,
    }));
  await expect.poll(async () => (await status())?.status, { message: "la machine en séance" }).toBe("in_session");

  // The deletion confirmed now is refused, with the reason.
  await confirm.getByRole("button", { name: say("fr", "common.delete") }).click();
  await expect(manager.getByText(say("fr", "errors.machine_in_session"))).toBeVisible();
  await confirm.getByRole("button", { name: say("fr", "common.cancel") }).click();
  // And the page no longer offers it while the session runs.
  await expect(deleteButton).toBeDisabled();

  // Nothing changed: still on its page, still listed, not deleted, the session still running.
  await expect(manager).toHaveURL(new RegExp(`/fr/dashboard/machines/${m1.id}$`));
  await manager.reload();
  await expect(manager.getByRole("heading", { name: m1.name })).toBeVisible();
  await expect(manager.getByText(say("fr", "machines.deleted"))).toHaveCount(0);
  await manager.goto("/fr/dashboard/machines");
  await expect(manager.getByRole("row", { name: new RegExp(escape(m1.name)) })).toBeVisible();
  expect((await status())?.isDeleted ?? false).toBe(false);
  expect((await pi.status()).body.run_state).toBe("running");
  expect((await sessionsOn(run, m1.id)).find((s) => s._id === sessionId)?.status).toBe("active");

  // Stopped at the console, as at the bench (its end is not this scenario's).
  expect((await pi.stopSession()).status).toBe(202);
});

test("ANH-157 un patient invité se connecte et retrouve sa fiche ; un second compte au même e-mail est refusé", async ({
  pageAs,
  browser,
  run,
}) => {
  test.setTimeout(150_000);
  const email = memberEmail(run, "invite");
  const invited = { firstName: "Ines", lastName: "Blot" };
  // In client B: the development instance holds at most 5 members per
  // organisation, and client A has 5 here (the invitation counts as one).
  const manager = await pageAs("gestionnaireB");

  // The manager invites the patient from the site.
  await manager.goto("/fr/dashboard/patients");
  await manager.getByRole("button", { name: say("fr", "users.createPatient") }).click();
  const form = manager.getByRole("dialog");
  await form.getByLabel(`${say("fr", "users.firstName")} *`, { exact: true }).fill(invited.firstName);
  await form.getByLabel(`${say("fr", "users.lastName")} *`, { exact: true }).fill(invited.lastName);
  await form.getByLabel(`${say("fr", "users.email")} *`, { exact: true }).fill(email);
  await form.getByRole("button", { name: say("fr", "common.create") }).click();
  const sent = manager.getByText(say("fr", "feedback.invitationSent", { email }));
  // The form's own refusal, said in full if it comes instead.
  const refused = form.locator(".text-destructive");
  await expect(sent.or(refused).first()).toBeVisible({ timeout: 30_000 });
  if ((await refused.count()) > 0) throw new Error(`invitation refusée : ${(await refused.allTextContents()).join(" | ")}`);

  // The record made at the invitation.
  const listed = await callAs<{ _id: string; email: string }[]>(run.backend, as(run, "admin"), "query", "users:listUsers", {
    role: "user",
  });
  const recorded = listed.filter((user) => user.email === email);
  expect(recorded, "une fiche créée à l'invitation").toHaveLength(1);

  // The invited person's Clerk account, its membership mirrored as the webhook would.
  const member = await addMember(run, { name: "invite", ...invited, organization: "clientB", role: "org:patient" });
  expect(member.convexId, "le compte est relié à la fiche de l'invitation").toBe(recorded[0]._id);

  const storageState = await signInMember(browser, run, { userId: member.userId, organization: "clientB", firstName: invited.firstName });
  const context = await browser.newContext({ baseURL: run.baseURL, storageState });
  try {
    const patient = await context.newPage();
    await patient.goto("/fr/dashboard/my-machines");
    await expect(patient.getByRole("heading", { name: say("fr", "training.myMachines.title") })).toBeVisible();
    await expect(patient.getByText(say("fr", "training.myMachines.noRights"))).toBeVisible();
  } finally {
    await context.close();
  }

  // One record only, the same, in the manager's list.
  await manager.goto("/fr/dashboard/patients");
  const rows = manager.getByRole("row").filter({ hasText: email });
  await expect(rows).toHaveCount(1);
  await expect(rows.getByRole("link").first()).toHaveAttribute("href", new RegExp(`${recorded[0]._id}$`));

  // A second account at the same address: Clerk refuses it, the list is unchanged.
  const second = await createAccountAt(email);
  expect(second).not.toBe("created");
  expect(second).toMatch(/HTTP 4\d\d/);
  await manager.reload();
  await expect(manager.getByRole("row").filter({ hasText: email })).toHaveCount(1);
});

test("ANH-167 la page Audit : lignes du scénario, filtres, export CSV relu, organisation seule, refus", async ({ pageAs, run }) => {
  test.setTimeout(180_000);
  const m1 = required(state.m1, "M1");
  const m3 = required(state.m3, "M3");
  const rows = (page: Page) => page.locator("tbody tr");
  /** The rows whose action cell says exactly this. */
  const withAction = (page: Page, text: string) => rows(page).filter({ has: page.getByRole("cell", { name: text, exact: true }) });
  const exactly = (text: string) => new RegExp(`^${escape(text)}$`);
  const label = (page: Page, key: string, lang: "fr" | "en" = "fr") => page.getByLabel(say(lang, key), { exact: true });
  const reset = (page: Page) => page.getByRole("button", { name: say("fr", "audit.filters.reset") }).click();
  /** The CSV of what the filters show, read back from the download (kept nowhere else). */
  const exported = async (page: Page) => {
    const download = page.waitForEvent("download");
    await page.getByRole("button", { name: say("fr", "audit.export") }).click();
    const csv = readFileSync(await (await download).path(), "utf8");
    return { csv, lines: csv.replace(/^\uFEFF/, "").trimEnd().split("\r\n") };
  };

  const admin = await pageAs("admin");
  await admin.goto("/fr/dashboard/audit");
  await expect(admin.getByRole("heading", { name: say("fr", "audit.title") })).toBeVisible();
  await expect(rows(admin).first()).toBeVisible();

  // Everything exported: the header, and no health value in any detail (no
  // heart rate, birth year, fitness answer or feedback score), names only.
  const all = await exported(admin);
  expect(all.lines[0]).toBe(AUDIT_CSV_COLUMNS.join(","));
  expect(all.lines.length).toBeGreaterThan(10);
  expect(all.csv).not.toMatch(/"(hrMax|birthYear|bpm|subjectHrMax|subjectAge|answers|nausea_score|dizziness_score|free_text)""?\s*:/);

  // By action: the rows of this file's actions, and nothing else.
  for (const [action, target] of [
    ["machine.created", m1.name],
    ["launch_right.granted", nameOf(run, "passagerA")],
    ["session.launched", null],
    ["gestionnaire_machines.set", `${MANAGER2.firstName} ${MANAGER2.lastName}`],
    ["session.started", null],
  ] as const) {
    const text = say("fr", `audit.actions.${action}`);
    await choose(admin, label(admin, "audit.filters.action"), exactly(text));
    await expect(rows(admin).first(), action).toBeVisible();
    const shown = await rows(admin).count();
    await expect(withAction(admin, text), `seulement ${action}`).toHaveCount(shown);
    if (target) await expect(rows(admin).filter({ hasText: target }).first(), `${action} : ${target}`).toBeVisible();
  }

  // Machine creations, exported as shown and read back.
  const created = say("fr", "audit.actions.machine.created");
  await choose(admin, label(admin, "audit.filters.action"), exactly(created));
  await expect(rows(admin).filter({ hasText: m1.name })).toHaveCount(1);
  await expect(rows(admin).filter({ hasText: m3.name })).toHaveCount(1);
  const creations = await rows(admin).count();
  const csv = await exported(admin);
  expect(csv.lines[0]).toBe(AUDIT_CSV_COLUMNS.join(","));
  expect(csv.lines.length - 1, "une ligne par ligne affichée").toBe(creations);
  expect(csv.lines.slice(1).every((line) => line.includes(",machine.created,"))).toBe(true);
  expect(csv.lines.some((line) => line.includes(m1.name))).toBe(true);

  // By organisation: client B's creations only.
  await choose(admin, label(admin, "audit.filters.organization"), exactly(`e2e-${run.runId} Client B`));
  await expect(rows(admin).filter({ hasText: m3.name })).toHaveCount(1);
  await expect(rows(admin).filter({ hasText: m1.name })).toHaveCount(0);

  // By type of target: machine keys only.
  await reset(admin);
  await choose(admin, label(admin, "audit.filters.targetType"), exactly(say("fr", "audit.targets.machine_key")));
  await expect(rows(admin).first()).toBeVisible();
  await expect(rows(admin).filter({ hasText: /Clé de machine|Clé expirée/ })).toHaveCount(await rows(admin).count());

  // By actor, then by target: the client A admin's changes of the second manager's machines.
  await reset(admin);
  await choose(admin, label(admin, "audit.filters.actor"), exactly(nameOf(run, "orgAdminA")));
  await expect(rows(admin).first()).toBeVisible();
  await expect(rows(admin).filter({ hasText: nameOf(run, "orgAdminA") })).toHaveCount(await rows(admin).count());
  const changed = say("fr", "audit.actions.gestionnaire_machines.set");
  await expect(withAction(admin, changed)).toHaveCount(2);
  await withAction(admin, changed).first().getByRole("button", { name: say("fr", "audit.filterTarget") }).click();
  await expect(withAction(admin, changed)).toHaveCount(2);
  await expect(rows(admin).filter({ hasText: `${MANAGER2.firstName} ${MANAGER2.lastName}` })).toHaveCount(await rows(admin).count());

  // By period: two days long past hold nothing; today, the rows again.
  await reset(admin);
  await label(admin, "audit.filters.from").fill("2020-01-01");
  await label(admin, "audit.filters.to").fill("2020-01-02");
  await expect(admin.getByText(say("fr", "audit.empty"))).toBeVisible();
  await expect(rows(admin).filter({ hasText: m1.name })).toHaveCount(0);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
  await label(admin, "audit.filters.from").fill(today);
  await label(admin, "audit.filters.to").fill(today);
  await expect(rows(admin).first()).toBeVisible();

  // The client A admin, in English: its organisation only, no organisation filter.
  const orgAdmin = await pageAs("orgAdminA");
  await orgAdmin.goto("/en/dashboard/audit");
  await choose(orgAdmin, label(orgAdmin, "audit.filters.action", "en"), exactly(say("en", "audit.actions.machine.created")));
  await expect(rows(orgAdmin).filter({ hasText: m1.name })).toHaveCount(1);
  await expect(rows(orgAdmin).filter({ hasText: m3.name })).toHaveCount(0);
  await expect(label(orgAdmin, "audit.filters.organization", "en")).toHaveCount(0);

  // Refused to a manager and to a passenger, with nothing shown or exported.
  for (const account of ["gestionnaireA", "passagerA"] as const) {
    const page = await pageAs(account);
    await page.goto("/fr/dashboard/audit");
    await expect(page.getByText(say("fr", "audit.accessDenied"))).toBeVisible();
    await expect(page.getByRole("button", { name: say("fr", "audit.export") })).toHaveCount(0);
    await expect(rows(page)).toHaveCount(0);
  }
});
