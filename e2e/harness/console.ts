import type { ChildProcess } from "node:child_process";
import { mkdirSync, rmSync } from "node:fs";
import path from "node:path";
import { freePort, startProcess, stopGroup, waitForHttp } from "./processes";
import { assertLocal } from "./secrets";

const OPERATOR = "Operateur E2E";

export type ConsoleAnswer<T = unknown> = { status: number; body: T };

/**
 * The real Pi console (`python -m src.local_panel`) in full simulation, on a
 * free loopback port, its data in the run folder, linked to the local backend
 * by the key the test gives it.
 */
export async function startConsole(options: {
  runDir: string;
  repoRoot: string;
  convexSiteUrl: string;
  machineApiKey: string;
}): Promise<PiConsole> {
  const port = await freePort();
  const name = `console-${port}`;
  const data = path.join(options.runDir, name);
  mkdirSync(data, { recursive: true });
  const piRoot = path.join(options.repoRoot, "raspberry-pi");
  const child = startProcess(options.runDir, name, path.join(piRoot, ".venv", "bin", "python"), ["-m", "src.local_panel"], {
    cwd: piRoot,
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
      CONVEX_URL: assertLocal(options.convexSiteUrl),
      MACHINE_API_KEY: options.machineApiKey,
    },
  });
  const url = `http://127.0.0.1:${port}`;
  await waitForHttp(`${url}/healthz`, { timeoutMs: 60_000, child, runDir: options.runDir, name });
  return new PiConsole(url, port, child, options.runDir, data);
}

/** The console's HTTP API, as the operator's page and `simulation/endurance.py` use it. */
export class PiConsole {
  constructor(
    readonly url: string,
    readonly port: number,
    private readonly child: ChildProcess,
    private readonly runDir: string,
    private readonly data: string,
  ) {}

  async get<T>(route: string): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, { signal: AbortSignal.timeout(10_000) });
    return { status: response.status, body: (await response.json()) as T };
  }

  async post<T>(route: string, body: Record<string, unknown>): Promise<ConsoleAnswer<T>> {
    const response = await fetch(`${this.url}${route}`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    return { status: response.status, body: (await response.json()) as T };
  }

  status = () => this.get<Record<string, unknown>>("/api/status");
  panel = () => this.get<{ dashboard: { state: string } | null }>("/api/panel");
  remoteLaunch = () => this.get<{ session_id: string; can_confirm: boolean } | null>("/api/remote-launch");
  attest = () =>
    this.post("/api/safety/attest", { operator: OPERATOR, sto_jumper_removed: true, mushroom_wired_nc: true });
  confirmLaunch = (sessionId: string) =>
    this.post("/api/remote-launch/confirm", { session_id: sessionId, operator: OPERATOR });
  stopSession = (reason = "arret E2E") => this.post("/api/session/stop", { operator: OPERATOR, reason });
  presence = () => this.post("/api/presence", { operator: OPERATOR });
  /** `{nausea_score, dizziness_score, comfort, free_text?}`, docs/console-locale.md section 14. */
  feedback = (answer: Record<string, unknown>) => this.post("/api/feedback", answer);

  /** Ctrl-C as at the bench: the console releases the drive and exits. */
  async stop(): Promise<number | null> {
    if (this.child.exitCode !== null) return this.child.exitCode;
    const exited = new Promise<number | null>((resolve) => this.child.once("exit", resolve));
    await stopGroup(this.child.pid as number, "SIGINT");
    const code = await exited;
    rmSync(path.join(this.runDir, "pids", String(this.child.pid)), { force: true });
    rmSync(this.data, { recursive: true, force: true });
    return code;
  }
}
