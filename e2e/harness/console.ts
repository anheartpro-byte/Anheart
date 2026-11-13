import type { ChildProcess } from "node:child_process";
import { createHash, generateKeyPairSync, randomBytes, randomInt, scryptSync, sign } from "node:crypto";
import { cpSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import path from "node:path";
import { freePort, startProcess, stopGroup, waitForHttp } from "./processes";
import { shippedProfiles } from "./profiles";
import { assertLocal } from "./secrets";

/** The operator every console of a run signs in, from the list the harness signs for it (ANH-152). */
export const E2E_OPERATOR = { userId: "operateur-e2e", name: "Operateur E2E" } as const;

/**
 * The catalogue public key of a console copy: each start replaces it by the
 * public half of a pair made for that start, whose private half signs the
 * console's operator list (`plantOperatorList`). Left out of the digest.
 */
export const CATALOG_KEY = path.join("config", "anheart_catalog.pub");

/** What the console runs from: its code, its signed configuration, its version. */
const CONSOLE_PARTS = ["src", "config", "VERSION"];
const skipped = (file: string) => file.includes("__pycache__") || file.endsWith(".pyc");

function* treeFiles(root: string, relative: string): Generator<string> {
  const full = path.join(root, relative);
  if (skipped(relative)) return;
  if (!statSync(full).isDirectory()) {
    yield relative;
    return;
  }
  for (const entry of readdirSync(full).sort()) yield* treeFiles(root, path.join(relative, entry));
}

/**
 * One digest of the console's files under `root` (`raspberry-pi/` or a copy of
 * it), the catalogue public key aside: each start replaces it (`CATALOG_KEY`).
 */
export function consoleTreeDigest(root: string): string {
  const hash = createHash("sha256");
  for (const part of CONSOLE_PARTS) {
    for (const file of treeFiles(root, part)) {
      if (file === CATALOG_KEY) continue;
      hash.update(file).update("\0").update(readFileSync(path.join(root, file)));
    }
  }
  return hash.digest("hex");
}

/**
 * A console opens no action without the signed list of its operators
 * (ANH-152), and the local backend signs none. So each start makes an Ed25519
 * pair, puts its public half in place of the copy's catalogue key, and writes
 * in the copy's data a list of one operator, `E2E_OPERATOR`, with a code drawn
 * now, signed by the private half as Convex signs it. Nothing of the pair or
 * of the code outlives the process but the copy's files; the code is returned.
 */
function plantOperatorList(root: string): string {
  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  writeFileSync(path.join(root, CATALOG_KEY), publicKey.export({ type: "spki", format: "pem" }));
  const code = String(randomInt(100_000, 1_000_000));
  const salt = randomBytes(16);
  // Cheap parameters: the console takes those the signed list carries.
  const [n, r, p] = [1024, 8, 1];
  const digest = scryptSync(code, salt, 32, { N: n, r, p });
  const payload = JSON.stringify({
    kind: "operator_list",
    machineId: "machine-e2e",
    issuedAt: Date.now(),
    operators: [
      {
        userId: E2E_OPERATOR.userId,
        name: E2E_OPERATOR.name,
        code: { algorithm: "scrypt", n, r, p, salt: salt.toString("base64"), hash: digest.toString("base64") },
      },
    ],
    devices: [],
  });
  const signature = sign(null, Buffer.from(payload, "utf8"), privateKey).toString("base64");
  mkdirSync(path.join(root, "data"), { recursive: true });
  writeFileSync(path.join(root, "data", "operators.json"), `${JSON.stringify({ schema_version: 1, payload, signature })}\n`);
  return code;
}

export type ConsoleAnswer<T = unknown> = { status: number; body: T };

/**
 * The real Pi console (`python -m src.local_panel`) in full simulation, on a
 * free loopback port, linked to the local backend by the key the test gives it.
 *
 * It runs from a copy of `raspberry-pi/` (code, signed configuration, version,
 * unchanged) made in the run folder at each start: what the console writes
 * next to its code (its profile store, `data/profiles.local.json`) stays
 * there, and no `raspberry-pi/.env` is read.
 *
 * Its catalogue public key is the one of the run, and it holds a signed list
 * of one operator (`plantOperatorList`, ANH-152): `PiConsole` signs that
 * operator in before its first action.
 *
 * `profiles`: programmes the console holds besides its shipped ones, written
 * in that copy's store before it starts. No route of the console writes a
 * profile since ANH-138, and the local backend signs no catalogue: this is
 * how a test gives it one, as a store of no catalogue (its fallback).
 */
export async function startConsole(options: {
  runDir: string;
  repoRoot: string;
  /** The local backend's own address (its client API, `Backend.url`). */
  backendUrl: string;
  machineApiKey: string;
  profiles?: Record<string, unknown>[];
}): Promise<PiConsole> {
  const port = await freePort();
  const name = `console-${port}`;
  const data = path.join(options.runDir, name);
  const root = path.join(data, "raspberry-pi");
  const piRoot = path.join(options.repoRoot, "raspberry-pi");
  mkdirSync(root, { recursive: true });
  for (const part of CONSOLE_PARTS) {
    cpSync(path.join(piRoot, part), path.join(root, part), {
      recursive: true,
      filter: (source) => !skipped(source),
    });
  }
  const code = plantOperatorList(root);
  if (options.profiles !== undefined) {
    const store = { version: 1, rev: 1, profiles: [...shippedProfiles(options.repoRoot), ...options.profiles] };
    writeFileSync(path.join(root, "data", "profiles.local.json"), `${JSON.stringify(store, null, 2)}\n`);
  }
  const child = startProcess(options.runDir, name, path.join(piRoot, ".venv", "bin", "python"), ["-m", "src.local_panel"], {
    cwd: root,
    env: {
      PATH: process.env.PATH ?? "",
      HOME: process.env.HOME ?? "",
      TMPDIR: process.env.TMPDIR ?? "/tmp",
      LANG: "C.UTF-8",
      PYTHONDONTWRITEBYTECODE: "1",
      MOTOR_BACKEND: "sim",
      ECG_SOURCE: "sim",
      ARM_RADIUS_M: "1.5",
      PROGRAMS_ENABLED: "true",
      OCCUPANCY_OCCUPIED_ENABLED: "true",
      UI_HOST: "127.0.0.1",
      UI_PORT: String(port),
      RECORD_ROOT: path.join(data, "records"),
      // The HTTP actions under /http of the backend's own origin, which it also
      // serves: the console only uploads a record to storage on the origin of
      // CONVEX_URL (the cloud's *.convex.site to *.convex.cloud, same port),
      // and the local backend's storage is on its main port.
      CONVEX_URL: assertLocal(`${options.backendUrl}/http`),
      MACHINE_API_KEY: options.machineApiKey,
    },
  });
  const url = `http://127.0.0.1:${port}`;
  await waitForHttp(`${url}/healthz`, { timeoutMs: 60_000, child, runDir: options.runDir, name });
  return new PiConsole(url, port, root, child, options.runDir, data, code);
}

type Speeds = { motor_rpm: number; output_rpm: number };

/** The console's HTTP API, as the operator's page and `simulation/endurance.py` use it. */
export class PiConsole {
  private presenceTimer: NodeJS.Timeout | undefined;
  /** The session cookie of `E2E_OPERATOR`, once signed in (ANH-152). */
  private session: string | undefined;

  constructor(
    readonly url: string,
    readonly port: number,
    /** The copy of `raspberry-pi/` this console runs from. */
    readonly root: string,
    private readonly child: ChildProcess,
    private readonly runDir: string,
    private readonly data: string,
    /** The code of `E2E_OPERATOR` in this console's list. */
    private readonly code: string,
  ) {}

  /**
   * Every action takes an operator signed in (ANH-152): `E2E_OPERATOR` signs
   * in once, and the cookie goes with every action after. Reads take none.
   */
  private async signedIn(): Promise<string> {
    if (this.session === undefined) {
      const response = await fetch(`${this.url}/api/access/sign-in`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ user_id: E2E_OPERATOR.userId, code: this.code }),
        signal: AbortSignal.timeout(10_000),
      });
      if (response.status !== 200) {
        throw new Error(`connexion refusée par la console : ${response.status} ${await response.text()}`);
      }
      this.session = (response.headers.get("set-cookie") ?? "").split(";")[0];
    }
    return this.session;
  }

  async get<T>(route: string): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, { signal: AbortSignal.timeout(10_000) });
    return { status: response.status, body: (await response.json()) as T };
  }

  async send<T>(method: "POST", route: string, body: Record<string, unknown>): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, {
      method,
      headers: { "content-type": "application/json", cookie: await this.signedIn() },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    return { status: response.status, body: (await response.json()) as T };
  }

  post<T>(route: string, body: Record<string, unknown>): Promise<ConsoleAnswer<T>> {
    return this.send<T>("POST", route, body);
  }

  status = () => this.get<{ run_state: string } & Record<string, unknown>>("/api/status");
  snapshot = () => this.get<{ measured: Speeds; phase: string } | null>("/api/snapshot");
  panel = () => this.get<{ dashboard: { state: string } | null }>("/api/panel");
  roster = () => this.get<{ state: string; riders: { user_id: string; name: string }[] }>("/api/roster");
  profiles = () => this.get<{ rev: number; profiles: Record<string, unknown>[] }>("/api/profiles");
  remoteLaunch = () =>
    this.get<{ session_id: string; state: string; reason: string | null; can_confirm: boolean } | null>(
      "/api/remote-launch",
    );
  attest = () => this.post("/api/safety/attest", { sto_jumper_removed: true, mushroom_wired_nc: true });
  confirmLaunch = (sessionId: string) => this.post("/api/remote-launch/confirm", { session_id: sessionId });
  /** The console's pre-flight (ANH-142), as its "Pre-vol" page reads it; `null` without one. */
  preflight = () =>
    this.get<{ running: boolean; occupied: { first_block: string | null } } | null>("/api/preflight");

  /**
   * Until the console's pre-flight lets a start with a person on board (ANH-142): its run
   * over, no blocking control red. A run reads the BITalino's battery (the acquisition
   * paused) when the console starts and after each session, then wants 30 s of good ECG:
   * some 40 s after a start, at once on a console that has been at rest a while. A
   * programme confirmed sooner is refused by the console (`refusee par la machine : pre-vol
   * : ...`). Call it after the attestation, which is one of the controls.
   */
  async untilReadyOnBoard(timeoutMs = 90_000): Promise<void> {
    const deadline = Date.now() + timeoutMs;
    let said = "aucune réponse";
    while (Date.now() < deadline) {
      const preflight = (await this.preflight()).body;
      if (preflight === null) return;
      if (!preflight.running && preflight.occupied.first_block === null) return;
      said = preflight.running ? "pré-vol en cours" : (preflight.occupied.first_block ?? "");
      await new Promise((resolve) => setTimeout(resolve, 1_000));
    }
    throw new Error(`le pré-vol refuse encore une personne à bord : ${said}`);
  }
  /** "Demarrer MANUEL" on the bench, nobody on board, target 0 (`simulation/endurance.py`, `start_manual`). */
  manualStart = () => this.post("/api/manual/start", { occupancy: "bench" });
  stopSession = (reason = "arret E2E") => this.post("/api/session/stop", { reason });
  presence = () => this.post("/api/presence", {});
  /** The "Retour du passager" card: `{current: {status, can_submit, ...} | null}`, never the values. */
  feedbackCard = () =>
    this.get<{ current: { status: string; status_text: string; can_submit: boolean } | null }>("/api/feedback");
  /** `{nausea_score, dizziness_score, comfort, free_text?}`, docs/console-locale.md section 14. */
  feedback = (answer: Record<string, unknown>) => this.post("/api/feedback", answer);

  /**
   * The attendant at the console, as the open page says it every 5 s: a
   * session without it is frozen at 60 s and stopped at 120 s.
   */
  keepPresent(): void {
    if (this.presenceTimer) return;
    const ping = () => void this.presence().catch(() => undefined);
    ping();
    this.presenceTimer = setInterval(ping, 5_000);
  }

  /** Ctrl-C as at the bench: the console releases the drive and exits. */
  async stop(): Promise<number | null> {
    clearInterval(this.presenceTimer);
    this.presenceTimer = undefined;
    // Already gone, by itself or by a signal (an earlier stop's SIGKILL): its exit was emitted.
    if (this.child.exitCode !== null || this.child.signalCode !== null) return this.child.exitCode;
    const exited = new Promise<number | null>((resolve) => this.child.once("exit", resolve));
    await stopGroup(this.child.pid as number, "SIGINT");
    const code = await exited;
    rmSync(path.join(this.runDir, "pids", String(this.child.pid)), { force: true });
    rmSync(this.data, { recursive: true, force: true });
    return code;
  }
}
