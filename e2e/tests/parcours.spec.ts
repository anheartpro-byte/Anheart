import type { Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { expect, test } from "../fixtures";
import { consoleTreeDigest, startConsole, type PiConsole } from "../harness/console";
import { callAs } from "../harness/convex";
import { logTail, portIsFree } from "../harness/processes";
import { convexUserId, identityOf, type AccountKey, type RunInfo } from "../harness/run";
import { rememberFromTest } from "../harness/secrets";
import { card, choose, escape } from "../ui";

/*
 * The main journey, in the browser, with the real simulated console: machine
 * and key made on the site, console online, launch right given by a manager,
 * remote launch confirmed at the console, live session, stop from the site,
 * report, full recording, rider feedback, lists, offline alert.
 *
 * These pages show a passenger's health data, and the site shows a machine key
 * once: no trace, video or capture is kept, even on failure. No assertion
 * message carries a health value.
 */
test.describe.configure({ mode: "serial" });
test.use({ trace: "off", video: "off", screenshot: "off" });

type Medical = {
  hr_hard_max_bpm: number;
  hr_critical_bpm: number;
  min_rider_age: number;
  motion_limits: { min_run_motor_rpm: number };
  recovery_min_s: number;
  cooldown_min_s: number;
};

/** The signed medical file the console reads (Convex holds the same version). */
function medicalOf(run: RunInfo): Medical {
  const file = path.join(run.repoRoot, "raspberry-pi", "config", "medical_parameters.json");
  return (JSON.parse(readFileSync(file, "utf8")) as { parameters: Medical }).parameters;
}

/**
 * A programme short enough to watch to its end: the console's shipped
 * `standard_30_min` (its zone, reference subject and ceilings), with the tiers
 * and limits of the medical file. Only the durations are a test choice.
 */
function shortProfile(standard: Record<string, unknown>, medical: Medical) {
  // Test choice: 10 s at rest, up to 60 s of warm-up, 30 s of hold, 20 s of
  // cool-down, and the shortest recovery the medical file allows.
  const baseline_s = 10;
  const warmup_max_s = 60;
  const hold_min_s = 30;
  const cooldown_s = Math.max(20, medical.cooldown_min_s);
  const recovery_s = medical.recovery_min_s;
  return {
    profile_id: "e2e_court",
    name: "E2E court",
    total_duration_s: baseline_s + warmup_max_s + hold_min_s + cooldown_s + recovery_s,
    baseline_s,
    warmup_max_s,
    hold_min_s,
    cooldown_s,
    recovery_s,
    zone_low_bpm: standard.zone_low_bpm,
    zone_high_bpm: standard.zone_high_bpm,
    hard_max_bpm: medical.hr_hard_max_bpm,
    critical_bpm: medical.hr_critical_bpm,
    subject_hr_max: standard.subject_hr_max,
    min_run_rpm: medical.motion_limits.min_run_motor_rpm,
    max_rpm: standard.max_rpm,
    warmup_rpm_ceiling_fraction: standard.warmup_rpm_ceiling_fraction,
    channels: standard.channels,
    allow_above_nameplate: standard.allow_above_nameplate,
  };
}

const PROFILE_NAME = "E2E court";

/** The value of a live readout takes at least two values (never printed: one is a heart rate). */
async function expectChanging(page: Page, testId: string, timeout: number): Promise<void> {
  const seen = new Set<string>();
  await expect
    .poll(
      async () => {
        const value = ((await page.getByTestId(testId).textContent()) ?? "").trim();
        if (value !== "" && value !== "-") seen.add(value);
        return seen.size;
      },
      { message: `${testId} prend au moins deux valeurs`, timeout, intervals: [500] },
    )
    .toBeGreaterThan(1);
}

/** The full recording of a session, as the manager's site lists it (every page). */
async function recordFiles(run: RunInfo, account: AccountKey, sessionId: string) {
  const files: { id: string; path: string; sha256: string; size: number; verified: boolean }[] = [];
  let cursor: string | null = null;
  do {
    const page: { files: typeof files; cursor: string | null } | null = await callAs(
      run.backend,
      identityOf(run, account),
      "query",
      "recordStorage:list",
      { sessionId, cursor },
    );
    if (!page) throw new Error("enregistrement invisible pour ce compte");
    files.push(...page.files);
    cursor = page.cursor;
  } while (cursor);
  return files;
}

/** The Clerk token the site sends Convex for this signed-in page (kept in memory, masked). */
async function convexToken(page: Page): Promise<string> {
  await page.waitForFunction(() => Boolean((window as unknown as { Clerk?: { session?: unknown } }).Clerk?.session));
  const token = await page.evaluate(async () => {
    const clerk = (window as unknown as {
      Clerk: { session: { getToken(options: { template: string }): Promise<string | null> } };
    }).Clerk;
    return await clerk.session.getToken({ template: "convex" });
  });
  if (!token) throw new Error("aucun jeton de session");
  return rememberFromTest(token);
}

/** What the journey carries from one step to the next. */
const journey: {
  machineId?: string;
  machineName?: string;
  machineKey?: string;
  console?: PiConsole;
  sessionId?: string;
  /** When the console was back at rest after session 1: its deposit starts then. */
  idleAt?: number;
} = {};

function required<T>(value: T | undefined, what: string): T {
  if (value === undefined) throw new Error(`étape précédente manquante : ${what}`);
  return value;
}

/** The passenger launches the short programme from "Mes machines"; returns the session id. */
async function launchFromMyMachines(rider: Page, machineName: string): Promise<string> {
  await rider.goto("/fr/dashboard/my-machines");
  const machine = card(rider, machineName);
  const launch = machine.getByRole("button", { name: "Lancer une séance auto" });
  await expect(launch).toBeEnabled({ timeout: 30_000 });
  await launch.click();
  const dialog = rider.getByRole("dialog");
  await choose(rider, dialog.getByRole("combobox").first(), new RegExp(`^${escape(PROFILE_NAME)} ·`));
  await dialog.getByRole("button", { name: "Lancer", exact: true }).click();
  await expect(rider).toHaveURL(/\/fr\/dashboard\/sessions\/[^/]+\/live$/);
  await expect(rider.getByText("En attente que la machine arme la séance…")).toBeVisible();
  return rider.url().split("/").at(-2) as string;
}

/** The operator confirms the remote launch at the console, as its page does. */
async function confirmAtConsole(pi: PiConsole, sessionId: string): Promise<void> {
  await expect
    .poll(
      async () => {
        const offer = (await pi.remoteLaunch()).body;
        return offer?.session_id === sessionId && offer.can_confirm;
      },
      { message: "la console propose ce départ, confirmable", timeout: 30_000 },
    )
    .toBe(true);
  expect((await pi.confirmLaunch(sessionId)).status).toBe(202);
  // Not running yet: the console's state and the last word on the offer,
  // numbers masked (a refusal can quote a passenger's value).
  let lastOffer = "aucune";
  await expect
    .poll(
      async () => {
        const state = (await pi.status()).body.run_state;
        if (state === "running") return state;
        const offer = (await pi.remoteLaunch()).body;
        if (offer) lastOffer = `${offer.state} ${(offer.reason ?? "").replace(/\d+/g, "#")}`.trim();
        return `${state}, dernière offre : ${lastOffer}`;
      },
      { message: "la console tourne", timeout: 30_000 },
    )
    .toBe("running");
}

test.afterAll(async () => {
  await journey.console?.stop();
});

test("1. l'admin Anheart crée la machine du client A sur le site, puis sa clé", async ({ pageAs, run }) => {
  const page = await pageAs("admin");
  const name = `Machine parcours ${run.runId}`;
  const manager = run.accounts.gestionnaireA;

  await page.goto("/fr/dashboard/machines");
  await page.getByRole("button", { name: "Nouvelle machine" }).first().click();
  const form = page.getByRole("dialog");
  await form.getByLabel("Nom de la machine").fill(name);
  await choose(page, form.getByRole("combobox", { name: "Organisation" }), `e2e-${run.runId} Client A`);
  await form.getByRole("checkbox", { name: new RegExp(escape(manager.email)) }).check();
  await form.getByRole("button", { name: "Créer" }).click();
  // The first key, shown once at creation, is left unread: the card makes the one used.
  await expect(form.getByText("Cette clé ne sera affichée qu'une seule fois!")).toBeVisible();
  await form.getByRole("button", { name: "Fermer" }).click();

  await page.getByRole("row", { name: new RegExp(escape(name)) }).click();
  await expect(page).toHaveURL(/\/fr\/dashboard\/machines\/[^/]+$/);
  journey.machineId = page.url().split("/").at(-1);
  journey.machineName = name;
  await expect(page.getByRole("heading", { name })).toBeVisible();
  await expect(page.getByText("Hors ligne").first()).toBeVisible();

  const keys = card(page, "Clés de la machine");
  await expect(keys.getByText("Active", { exact: true })).toHaveCount(1);
  await keys.getByRole("button", { name: "Nouvelle clé" }).click();
  const create = page.getByRole("dialog");
  await expect(create.getByText("Nouvelle clé pour cette machine")).toBeVisible();
  await choose(
    page,
    create.getByRole("combobox", { name: "Chevauchement avec les clés actuelles" }),
    "Aucun : révoquer les clés actuelles tout de suite",
  );
  await create.getByRole("button", { name: "Créer la clé" }).click();
  const shown = page.getByRole("dialog");
  await expect(shown.getByText("Cette clé ne sera affichée qu'une seule fois!")).toBeVisible();
  // Read once, kept in memory and masked: never printed, traced or captured.
  const key = rememberFromTest(((await shown.locator(".font-mono").textContent()) ?? "").trim());
  expect(/^anh1\.[0-9a-f]{32}\.[0-9a-f]{64}$/.test(key), "la clé affichée a la forme d'une clé de machine").toBe(true);
  journey.machineKey = key;
  await shown.getByRole("button", { name: "Fermer" }).click();
  await expect(keys.getByText("Révoquée", { exact: true })).toHaveCount(1);
  await expect(keys.getByText("Active", { exact: true })).toHaveCount(1);
});

test("2. la console simulée démarre avec cette clé : machine en ligne, santé distante affichée", async ({
  pageAs,
  run,
}) => {
  const pi = await startConsole({
    runDir: run.runDir,
    repoRoot: run.repoRoot,
    backendUrl: run.backend.url,
    machineApiKey: required(journey.machineKey, "clé de la machine"),
  });
  journey.console = pi;
  expect(consoleTreeDigest(pi.root)).toBe(consoleTreeDigest(path.join(run.repoRoot, "raspberry-pi")));
  await expect
    .poll(async () => (await pi.panel()).body.dashboard?.state, { message: "le lien de la console", timeout: 30_000 })
    .toBe("reachable");

  const page = await pageAs("admin");
  await page.goto(`/fr/dashboard/machines/${required(journey.machineId, "machine")}`);
  await expect(page.getByText("En ligne").first()).toBeVisible({ timeout: 30_000 });
  const health = card(page, "Santé de la machine");
  await expect(health.getByText("Rapport récent")).toBeVisible({ timeout: 30_000 });
  await expect(health.getByRole("term").filter({ hasText: "Température CPU" })).toBeVisible();
  await expect(health.getByRole("term").filter({ hasText: "Liaison variateur" })).toBeVisible();
});

test("3. le gestionnaire rend le passager prêt et lui donne le droit de lancement", async ({ pageAs, run }) => {
  test.setTimeout(240_000);
  const pi = required(journey.console, "console");
  const rider = run.accounts.passagerA;
  const riderName = `${rider.firstName} ${rider.lastName}`;
  const riderId = await convexUserId(run, "passagerA");
  const managerId = await convexUserId(run, "gestionnaireA");
  const standard = (await pi.profiles()).body.profiles.find((profile) => profile.profile_id === "standard_30_min");
  if (!standard) throw new Error("profil livré standard_30_min absent de la console");
  const medical = medicalOf(run);
  // The rider is the shipped profile's reference subject, an adult by the medical file.
  const hrMax = Number(standard.subject_hr_max);
  const birthYear = new Date().getUTCFullYear() - medical.min_rider_age - 20;

  // The organisation's admin makes the gestionnaire this passenger's manager.
  const orgAdmin = await pageAs("orgAdminA");
  await orgAdmin.goto(`/fr/dashboard/gestionnaires/${managerId}`);
  await card(orgAdmin, "Patients").getByRole("button", { name: "Modifier" }).click();
  const assign = orgAdmin.getByRole("dialog");
  await expect(assign.getByText("Assigner des Patients")).toBeVisible();
  await assign.getByRole("checkbox", { name: new RegExp(escape(rider.email)) }).check();
  await assign.getByRole("button", { name: "Enregistrer" }).click();
  await expect(orgAdmin.getByText(/Patients enregistrés : 1 ajouté/)).toBeVisible();

  // The gestionnaire sets the physiology the console needs.
  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/patients/${riderId}`);
  const physiology = card(manager, "Physiologie");
  await physiology.getByLabel("FC max mesurée (bpm)").fill(String(hrMax));
  await physiology.getByLabel("Année de naissance").fill(String(birthYear));
  await physiology.getByRole("button", { name: "Enregistrer" }).click();
  await expect(manager.getByText("Physiologie enregistrée.")).toBeVisible();

  // The passenger fills the fitness questionnaire and signs the consent.
  const passenger = await pageAs("passagerA");
  await passenger.goto("/fr/dashboard/my-machines");
  const fitness = card(passenger, "Aptitude");
  const questions = fitness.getByRole("group");
  await expect(questions.first()).toBeVisible();
  for (let index = 0; index < (await questions.count()); index += 1) {
    await questions.nth(index).getByRole("button", { name: "Non", exact: true }).click();
  }
  await fitness.getByRole("button", { name: "Envoyer la saisie" }).click();
  await expect(fitness.getByText(/à valider\./)).toBeVisible();
  const consent = card(passenger, "Consentement");
  await consent.getByRole("checkbox", { name: "J'ai lu ce texte et j'y consens." }).check();
  await consent.getByRole("button", { name: "Signer" }).click();
  await expect(consent.getByText(/Signé le/)).toBeVisible();

  // The gestionnaire validates the fitness for a year.
  await manager.reload();
  const validity = new Date(Date.now() + 365 * 24 * 3600 * 1000).toISOString().slice(0, 10);
  const review = card(manager, "Aptitude");
  await review.getByLabel("Valide jusqu'au").fill(validity);
  await review.getByRole("button", { name: "Valider l'aptitude" }).click();
  await expect(review.getByText(/Apte jusqu'au/)).toBeVisible();

  // Then gives the launch right on the machine.
  await manager.goto(`/fr/dashboard/machines/${required(journey.machineId, "machine")}`);
  const rights = card(manager, "Droits de lancement");
  await choose(manager, rights.getByRole("combobox"), new RegExp(escape(rider.email)));
  await rights.getByRole("button", { name: "Accorder" }).click();
  await expect(manager.getByText("Droit de lancement accordé.")).toBeVisible();
  await expect(rights.getByText(riderName).first()).toBeVisible();

  // The console's passenger list carries them at its next request (every 60 s).
  await expect
    .poll(async () => (await pi.roster()).body.riders.some((entry) => entry.name === riderName), {
      message: "le passager est dans la liste de la console",
      timeout: 90_000,
      intervals: [2_000],
    })
    .toBe(true);
});

test("4. le passager lance une séance programmée, l'opérateur la confirme à la console", async ({
  pageAs,
  run,
}) => {
  test.setTimeout(120_000);
  const pi = required(journey.console, "console");

  // The short programme, through the console's own API and its validations.
  const listed = (await pi.profiles()).body;
  const standard = listed.profiles.find((profile) => profile.profile_id === "standard_30_min");
  if (!standard) throw new Error("profil livré standard_30_min absent de la console");
  const profile = shortProfile(standard, medicalOf(run));
  const stored = await pi.putProfile(profile, listed.rev);
  expect(stored.status, "le profil court est accepté par la console").toBe(200);
  // Written next to the code that runs: the copy in the run folder, never raspberry-pi/.
  expect(existsSync(path.join(pi.root, "data", "profiles.local.json"))).toBe(true);
  const inTree = path.join(run.repoRoot, "raspberry-pi", "data", "profiles.local.json");
  expect(!existsSync(inTree) || !readFileSync(inTree, "utf8").includes(profile.profile_id)).toBe(true);

  const rider = await pageAs("passagerA");
  const sessionId = await launchFromMyMachines(rider, required(journey.machineName, "machine"));
  journey.sessionId = sessionId;

  expect((await pi.attest()).status).toBe(200);
  pi.keepPresent();
  await confirmAtConsole(pi, sessionId);
  await expect(rider.getByText("En attente que la machine arme la séance…")).toBeHidden({ timeout: 30_000 });
  await expect(rider.getByRole("button", { name: "Arrêter la séance" })).toBeVisible();
});

test("5. la séance en direct avance, en français puis en anglais", async ({ pageAs }) => {
  test.setTimeout(240_000);
  const pi = required(journey.console, "console");
  const sessionId = required(journey.sessionId, "séance");
  const rider = await pageAs("passagerA");

  await rider.goto(`/fr/dashboard/sessions/${sessionId}/live`);
  // Each one twice at least: its readout and its chart.
  await expect.poll(() => rider.getByText("Vitesse du bras", { exact: true }).count()).toBeGreaterThanOrEqual(2);
  await expect.poll(() => rider.getByText(/^Fréquence cardiaque/).count()).toBeGreaterThanOrEqual(2);
  await expect(rider.getByText("Écoulé")).toBeVisible();
  // The arm moves once the warm-up's target has pulled ahead of the heart
  // rate: some 25 s into the warm-up, later on a loaded computer. Until then,
  // the console's phase says where it stands.
  await expect
    .poll(
      async () => {
        const snapshot = (await pi.snapshot()).body;
        return snapshot && snapshot.measured.output_rpm > 0 ? "en mouvement" : `phase ${snapshot?.phase ?? "inconnue"}`;
      },
      { message: "le bras tourne à la console", timeout: 120_000, intervals: [1_000] },
    )
    .toBe("en mouvement");
  await expectChanging(rider, "live-arm-speed", 60_000);
  await expect
    .poll(async () => /^\d+$/.test(((await rider.getByTestId("live-heart-rate").textContent()) ?? "").trim()), {
      message: "une fréquence cardiaque est affichée",
      timeout: 30_000,
    })
    .toBe(true);

  await rider.goto(`/en/dashboard/sessions/${sessionId}/live`);
  await expect.poll(() => rider.getByText("Arm speed", { exact: true }).count()).toBeGreaterThanOrEqual(2);
  await expect.poll(() => rider.getByText(/^Heart rate/).count()).toBeGreaterThanOrEqual(2);
  await expect(rider.getByRole("button", { name: "Stop session" })).toBeVisible();
  await expect(rider.getByTestId("live-arm-speed")).toHaveText(/^\d+\.\d$/);
  await expectChanging(rider, "live-elapsed", 15_000);
});

test("6. l'arrêt depuis le site ramène le bras à 0 et termine la séance", async ({ pageAs }) => {
  test.setTimeout(240_000);
  const pi = required(journey.console, "console");
  const sessionId = required(journey.sessionId, "séance");
  const rider = await pageAs("passagerA");
  await rider.goto(`/fr/dashboard/sessions/${sessionId}/live`);

  await rider.getByRole("button", { name: "Arrêter la séance" }).click();
  await rider.getByRole("dialog").getByRole("button", { name: "Arrêter la séance" }).click();
  await expect(rider.getByText("Arrêt demandé, décélération en cours…")).toBeVisible({ timeout: 15_000 });
  await expect
    .poll(async () => (await pi.status()).body.run_state, { message: "la console s'arrête", timeout: 30_000 })
    .toBe("stopping");
  await expect(rider.getByTestId("live-arm-speed")).toHaveText("0.0", { timeout: 90_000 });
  await expect
    .poll(async () => (await pi.snapshot()).body?.measured.output_rpm, {
      message: "la console mesure le bras à l'arrêt",
      timeout: 30_000,
    })
    .toBe(0);

  // The end reaches the site once the console's recovery is over.
  await expect(rider.getByText("Séance terminée").first()).toBeVisible({ timeout: 180_000 });
  await expect(rider.getByText(/arret demande depuis le tableau de bord/).first()).toBeVisible();
  await expect
    .poll(async () => (await pi.status()).body.run_state, { message: "la console au repos", timeout: 30_000 })
    .toBe("idle");
  journey.idleAt = Date.now();
});

test("7. le rapport de la séance", async ({ pageAs, run }) => {
  const sessionId = required(journey.sessionId, "séance");
  const rider = run.accounts.passagerA;
  const manager = await pageAs("gestionnaireA");

  await manager.goto(`/fr/dashboard/sessions/${sessionId}`);
  await expect(manager.getByRole("heading", { name: `${rider.firstName} ${rider.lastName}`, exact: true })).toBeVisible();
  await expect(manager.getByText("Terminée").first()).toBeVisible();
  await expect(manager.getByText("Motif de fin").first()).toBeVisible();
  const duration = manager.getByText(/^\d+m \d+s$/).first();
  await expect(duration).toBeVisible();
  const [minutes, seconds] = ((await duration.textContent()) ?? "").match(/\d+/g)?.map(Number) ?? [0, 0];
  // Launch, a minute or so of motion, the stop and its recovery: between 1 and 10 minutes.
  expect(minutes * 60 + seconds).toBeGreaterThan(60);
  expect(minutes * 60 + seconds).toBeLessThan(600);

  await manager.goto(`/fr/dashboard/sessions/${sessionId}/report`);
  await expect(manager.getByRole("heading", { name: "Rapport de séance d'entraînement" })).toBeVisible();
  await expect(manager.getByText(`${rider.firstName} ${rider.lastName}`).first()).toBeVisible();
  await expect(manager.getByText(required(journey.machineName, "machine")).first()).toBeVisible();
  await expect(manager.getByText(PROFILE_NAME).first()).toBeVisible();
  await expect(manager.getByText("Terminée").first()).toBeVisible();
});

test("8. l'enregistrement complet est déposé, vérifié et téléchargé", async ({ pageAs, run }) => {
  // Session 1 records some 600 files (an ECG block every 200 ms); the console
  // deposits 2 to 10 of them a second against the local backend, slower after
  // a refused batch: up to 600 s is waited for, on the deposit's progress.
  test.setTimeout(780_000);
  const sessionId = required(journey.sessionId, "séance");
  // The deposit runs once the console is at rest; its state on the server
  // first, with its progress, and on failure what the console's journal says
  // of it (a journal without heart rate or passenger, masked by the harness).
  const pi = required(journey.console, "console");
  const started = Date.now();
  try {
    await expect
      .poll(
        async () => {
          const state = await callAs<{ record: string; verifiedCount: number; expectedCount: number } | null>(
            run.backend,
            identityOf(run, "gestionnaireA"),
            "query",
            "recordStorage:list",
            { sessionId },
          );
          return state ? `${state.record} ${state.verifiedCount}/${state.expectedCount}` : "aucun";
        },
        { message: "état de l'enregistrement sur le serveur", timeout: 600_000, intervals: [2_000] },
      )
      .toMatch(/^complete /);
  } catch (error) {
    const journal = logTail(run.runDir, `console-${pi.port}`, 400)
      .split("\n")
      .filter((line) => /deposit|record|upload|storage|commit/i.test(line))
      .slice(-15)
      .map((line) => line.replace(/([?&][a-z_]*(?:token|key)=)[^&"\s\\]+/gi, "$1[masqué]"))
      .join("\n");
    throw new Error(`${(error as Error).message}\njournal de la console (dépôt) :\n${journal}`);
  }
  // The deposit's pace, for a trial against the real backend: files over the
  // time since the console came to rest, and the console's own answers by route
  // (a 429 or a 500 makes it wait 4 s, then twice as long after each failed batch).
  const answers = new Map<string, number>();
  const journal = logTail(run.runDir, `console-${pi.port}`, Number.MAX_SAFE_INTEGER);
  for (const [, route, status] of journal.matchAll(
    /HTTP Request: (?:GET|POST) \S*?(\/api\/machine\/records\/[a-z-]+|\/api\/storage\/upload)\S* \\"HTTP\/1\.1 (\d{3})/g,
  )) {
    answers.set(`${route} ${status}`, (answers.get(`${route} ${status}`) ?? 0) + 1);
  }
  const files = await recordFiles(run, "gestionnaireA", sessionId);
  const seconds = (Date.now() - required(journey.idleAt, "fin de la séance")) / 1000;
  const pace = `${files.length} fichiers en ${Math.round(seconds)} s depuis la fin de la séance, ${(files.length / seconds).toFixed(2)} fichier(s)/s`;
  console.log(`[parcours] dépôt : ${pace} ; attente de l'étape ${Math.round((Date.now() - started) / 1000)} s`);
  console.log(`[parcours] réponses vues par la console : ${[...answers].map(([key, count]) => `${key} x${count}`).join(" | ")}`);
  test.info().annotations.push({ type: "dépôt", description: pace });
  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/sessions/${sessionId}`);
  const record = card(manager, "Enregistrement complet");
  await expect(record.getByText("Complet et vérifié")).toBeVisible();
  await expect(record.getByText(/^(\d+) \/ \1 fichiers vérifiés$/)).toBeVisible();
  await expect(record.getByRole("button", { name: "Télécharger l’archive" })).toBeEnabled();

  // The browser's own download needs a Convex URL in .convex.cloud
  // (lib/session-record.ts, fetchRecordFile): each file is fetched by the site's
  // route instead, with the manager's own token.
  expect(files.length).toBeGreaterThan(2);
  const token = await convexToken(manager);
  let manifest: { files: string[]; session_id?: string | null } | null = null;
  for (const file of files) {
    const response = await fetch(
      `${run.backend.siteUrl}/api/records/download?fileId=${encodeURIComponent(file.id)}`,
      { headers: { authorization: `Bearer ${token}` } },
    );
    expect(response.status, `téléchargement de ${file.path}`).toBe(200);
    const bytes = Buffer.from(await response.arrayBuffer());
    expect(bytes.length, `taille de ${file.path}`).toBe(file.size);
    expect(createHash("sha256").update(bytes).digest("hex"), `empreinte de ${file.path}`).toBe(file.sha256);
    if (file.path === "manifest.json") manifest = JSON.parse(bytes.toString("utf8"));
  }
  if (!manifest) throw new Error("manifest.json absent de l'enregistrement");
  const listed = new Set(files.map((file) => file.path));
  expect(manifest.files.every((entry) => listed.has(entry)), "le manifeste ne nomme que des fichiers déposés").toBe(true);
  expect(manifest.session_id ?? sessionId).toBe(sessionId);

  // A manager of another organisation is refused the same file.
  const other = await pageAs("gestionnaireB");
  await other.goto("/fr/dashboard");
  const refused = await fetch(
    `${run.backend.siteUrl}/api/records/download?fileId=${encodeURIComponent(files[0].id)}`,
    { headers: { authorization: `Bearer ${await convexToken(other)}` } },
  );
  expect([401, 403, 404]).toContain(refused.status);
});

test("9. le retour du passager saisi à la console apparaît sur la séance", async ({ pageAs, run }) => {
  test.setTimeout(120_000);
  const pi = required(journey.console, "console");
  const sessionId = required(journey.sessionId, "séance");
  await expect
    .poll(async () => (await pi.feedbackCard()).body.current?.can_submit ?? false, {
      message: "la carte Retour du passager est ouverte",
      timeout: 30_000,
    })
    .toBe(true);
  // A comment made for this run: counted at the end in everything the run leaves.
  const comment = rememberFromTest(`e2e-retour-${run.runId}`);
  const sent = await pi.feedback({ nausea_score: 1, dizziness_score: 0, comfort: "good", free_text: comment });
  expect(sent.status).toBe(202);
  await expect
    .poll(async () => (await pi.feedbackCard()).body.current?.status, {
      message: "le retour est envoyé au tableau de bord",
      timeout: 60_000,
    })
    .toBe("sent");

  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/sessions/${sessionId}`);
  const feedback = card(manager, "Retour du passager");
  await expect(feedback.getByText("Saisi par")).toBeVisible();
  await expect(feedback.getByText("L'opérateur, à la machine")).toBeVisible();
  await expect(feedback.getByText("Non renseigné")).toHaveCount(0);
});

test("10. la séance est dans les listes, et invisible pour une autre organisation", async ({ pageAs, run }) => {
  const sessionId = required(journey.sessionId, "séance");
  const machineName = required(journey.machineName, "machine");
  const rider = run.accounts.passagerA;
  const riderName = `${rider.firstName} ${rider.lastName}`;
  const manager = await pageAs("gestionnaireA");

  await manager.goto("/fr/dashboard/sessions");
  await expect(
    manager.getByRole("row").filter({ hasText: riderName }).filter({ hasText: machineName }).filter({ hasText: "Terminée" }),
  ).toHaveCount(1);

  await manager.goto(`/fr/dashboard/patients/${await convexUserId(run, "passagerA")}`);
  const history = card(manager, "Historique et progression");
  await expect(history.getByRole("row").filter({ hasText: PROFILE_NAME })).toHaveCount(1);

  await manager.goto(`/fr/dashboard/machines/${required(journey.machineId, "machine")}`);
  await expect(card(manager, "Droits de lancement").getByText(riderName).first()).toBeVisible();

  const other = await pageAs("gestionnaireB");
  await other.goto(`/fr/dashboard/sessions/${sessionId}`);
  await expect(other.getByText("Session introuvable")).toBeVisible();
});

test("6b. une seconde séance va jusqu'à sa fin normale, le passager donne son retour sur le site", async ({
  pageAs,
}) => {
  test.setTimeout(420_000);
  const pi = required(journey.console, "console");
  const rider = await pageAs("passagerA");
  const sessionId = await launchFromMyMachines(rider, required(journey.machineName, "machine"));
  await confirmAtConsole(pi, sessionId);
  await expectChanging(rider, "live-arm-speed", 60_000);

  // Its whole programme, recovery included, then the end on the site.
  await expect(rider.getByText("Séance terminée").first()).toBeVisible({ timeout: 330_000 });
  await expect(rider.getByText(/programme_complete/).first()).toBeVisible();
  await expect
    .poll(async () => (await pi.status()).body.run_state, { message: "la console au repos", timeout: 30_000 })
    .toBe("idle");

  await rider.goto(`/fr/dashboard/sessions/${sessionId}`);
  const feedback = card(rider, "Retour du passager");
  await choose(rider, feedback.getByRole("combobox", { name: "Nausée" }), /^0 - /);
  await choose(rider, feedback.getByRole("combobox", { name: "Malaise ou vertige" }), /^0 - /);
  await choose(rider, feedback.getByRole("combobox", { name: "Ressenti" }), /^Bon$/);
  await feedback.getByRole("button", { name: "Enregistrer le retour" }).click();
  await expect(rider.getByText("Retour enregistré")).toBeVisible();
  await expect(feedback.getByText("Le passager", { exact: true })).toBeVisible();
});

test("11. console arrêtée : la machine passe hors ligne et le gestionnaire reçoit l'alerte", async ({ pageAs }) => {
  test.setTimeout(330_000);
  const pi = required(journey.console, "console");
  const machineName = required(journey.machineName, "machine");
  expect(await pi.stop()).toBe(0);
  expect(await portIsFree(pi.port)).toBe(true);

  const manager = await pageAs("gestionnaireA");
  await manager.goto(`/fr/dashboard/machines/${required(journey.machineId, "machine")}`);
  // The site judges 90 s without a heartbeat on the server's clock.
  await expect(manager.getByText("Hors ligne").first()).toBeVisible({ timeout: 150_000 });

  // The alert comes from the server's minute task once those 90 s are past.
  await manager.goto("/fr/dashboard/notifications");
  const alert = manager.locator('li[data-alert="machine_offline"]').filter({ hasText: machineName });
  await expect(alert).toBeVisible({ timeout: 240_000 });
  await expect(alert.getByText("Machine hors ligne")).toBeVisible();
  await expect(manager.getByRole("link", { name: /^Alertes : \d+ non lues?$/ })).toBeVisible();
});
