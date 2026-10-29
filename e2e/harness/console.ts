import type { ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { cpSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync } from "node:fs";
import path from "node:path";
import { freePort, startProcess, stopGroup, waitForHttp } from "./processes";
import { assertLocal } from "./secrets";

const OPERATOR = "Operateur E2E";

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

/** One digest of the console's files under `root` (`raspberry-pi/` or a copy of it). */
export function consoleTreeDigest(root: string): string {
  const hash = createHash("sha256");
  for (const part of CONSOLE_PARTS) {
    for (const file of treeFiles(root, part)) hash.update(file).update("\0").update(readFileSync(path.join(root, file)));
  }
  return hash.digest("hex");
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
 */
export async function startConsole(options: {
  runDir: string;
  repoRoot: string;
  /** The local backend's own address (its client API, `Backend.url`). */
  backendUrl: string;
  machineApiKey: string;
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
  return new PiConsole(url, port, root, child, options.runDir, data);
}

type Speeds = { motor_rpm: number; output_rpm: number };

/** The console's HTTP API, as the operator's page and `simulation/endurance.py` use it. */
export class PiConsole {
  private presenceTimer: NodeJS.Timeout | undefined;

  constructor(
    readonly url: string,
    readonly port: number,
    /** The copy of `raspberry-pi/` this console runs from. */
    readonly root: string,
    private readonly child: ChildProcess,
    private readonly runDir: string,
    private readonly data: string,
  ) {}

  async get<T>(route: string): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, { signal: AbortSignal.timeout(10_000) });
    return { status: response.status, body: (await response.json()) as T };
  }

  async send<T>(method: "POST" | "PUT", route: string, body: Record<string, unknown>): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, {
      method,
      headers: { "content-type": "application/json" },
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
  putProfile = (document: Record<string, unknown> & { profile_id: string }, rev: number) =>
    this.send("PUT", `/api/profiles/${document.profile_id}?rev=${rev}`, document);
  remoteLaunch = () =>
    this.get<{ session_id: string; state: string; reason: string | null; can_confirm: boolean } | null>(
      "/api/remote-launch",
    );
  attest = () =>
    this.post("/api/safety/attest", { operator: OPERATOR, sto_jumper_removed: true, mushroom_wired_nc: true });
  confirmLaunch = (sessionId: string) =>
    this.post("/api/remote-launch/confirm", { session_id: sessionId, operator: OPERATOR });
  stopSession = (reason = "arret E2E") => this.post("/api/session/stop", { operator: OPERATOR, reason });
  presence = () => this.post("/api/presence", { operator: OPERATOR });
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
    if (this.child.exitCode !== null) return this.child.exitCode;
    const exited = new Promise<number | null>((resolve) => this.child.once("exit", resolve));
    await stopGroup(this.child.pid as number, "SIGINT");
    const code = await exited;
    rmSync(path.join(this.runDir, "pids", String(this.child.pid)), { force: true });
    rmSync(this.data, { recursive: true, force: true });
    return code;
  }
}
