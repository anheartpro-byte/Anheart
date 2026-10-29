import { spawn, type ChildProcess } from "node:child_process";
import {
  chmodSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  openSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { redact } from "./secrets";

/**
 * A folder of the harness, private to this user: created 0700, refused when
 * it is a symbolic link, not a folder, or someone else's.
 */
export function privateDir(dir: string): string {
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  const stat = lstatSync(dir);
  if (stat.isSymbolicLink() || !stat.isDirectory()) throw new Error(`${dir} : pas un dossier (ou un lien symbolique), refusé`);
  if (stat.uid !== process.getuid?.()) throw new Error(`${dir} : n'appartient pas à cet utilisateur, refusé`);
  if ((stat.mode & 0o077) !== 0) chmodSync(dir, 0o700);
  return dir;
}

/**
 * The harness's own folder, `$XDG_CACHE_HOME/anheart-e2e`, else
 * `~/.cache/anheart-e2e`: its runs, and its outputs unless E2E_OUTPUT_DIR
 * says otherwise. Never the system's shared temporary folder.
 */
export function harnessRoot(): string {
  const cache = process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache");
  return privateDir(path.join(cache, "anheart-e2e"));
}

/** Every run works in its own folder under this one, removed at its end. */
export const runsRoot = () => privateDir(path.join(harnessRoot(), "executions"));

/** A file of the run folder, created by this run alone (it must not exist yet), 0600. */
export function writeNewFile(file: string, content: string | Buffer): void {
  writeFileSync(file, content, { flag: "wx", mode: 0o600 });
}

/** The run's folder, of a name nobody can guess (`<runId>-<random>`), 0700. */
export function newRunDir(runId: string): string {
  const dir = mkdtempSync(path.join(runsRoot(), `${runId}-`));
  mkdirSync(path.join(dir, "pids"), { mode: 0o700 });
  writeNewFile(path.join(dir, "owner"), String(process.pid));
  return dir;
}

function alive(pid: number, group = false): boolean {
  try {
    process.kill(group ? -pid : pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code === "EPERM";
  }
}

export async function freePort(): Promise<number> {
  return await new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const { port } = server.address() as net.AddressInfo;
      server.close(() => resolve(port));
    });
  });
}

/** Nothing listens on `port` of the loopback, IPv4 or IPv6. */
export async function portIsFree(port: number): Promise<boolean> {
  return await new Promise((resolve) => {
    const socket = net.connect({ host: "localhost", port, autoSelectFamily: true });
    socket.once("connect", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => resolve(true));
  });
}

export function logTail(runDir: string, name: string, lines = 30): string {
  const file = path.join(runDir, `${name}.log`);
  if (!existsSync(file)) return "";
  return redact(readFileSync(file, "utf8").split("\n").slice(-lines).join("\n"));
}

/**
 * Starts a process in its own process group, its output in `<name>.log` of the
 * run folder, its pid in `pids/` so that the end of this run, or the start of
 * the next one after a crash, stops it and everything it started.
 */
export function startProcess(
  runDir: string,
  name: string,
  command: string,
  args: string[],
  options: { cwd: string; env: Record<string, string | undefined> },
): ChildProcess {
  const log = openSync(path.join(runDir, `${name}.log`), "a", 0o600);
  const child = spawn(command, args, {
    cwd: options.cwd,
    env: options.env as NodeJS.ProcessEnv,
    detached: true,
    stdio: ["ignore", log, log],
  });
  if (child.pid === undefined) throw new Error(`${name} : démarrage impossible`);
  writeNewFile(path.join(runDir, "pids", String(child.pid)), name);
  return child;
}

/** Runs a command to its end; its exit code must be 0. */
export async function runToEnd(
  runDir: string,
  name: string,
  command: string,
  args: string[],
  options: { cwd: string; env: Record<string, string | undefined> },
): Promise<void> {
  const child = startProcess(runDir, name, command, args, options);
  const code = await new Promise<number | null>((resolve) =>
    child.once("exit", resolve),
  );
  rmSync(path.join(runDir, "pids", String(child.pid)), { force: true });
  if (code !== 0) {
    throw new Error(`${name} a échoué (code ${code}) :\n${logTail(runDir, name)}`);
  }
}

/** SIGTERM to the whole group (SIGINT for a process that stops on it), then SIGKILL. */
export async function stopGroup(
  pid: number,
  signal: NodeJS.Signals = "SIGTERM",
  graceMs = 15_000,
): Promise<void> {
  try {
    process.kill(-pid, signal);
  } catch {
    return;
  }
  const deadline = Date.now() + graceMs;
  while (alive(pid, true) && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  try {
    process.kill(-pid, "SIGKILL");
  } catch {
    // already gone
  }
}

export async function stopRunProcesses(runDir: string): Promise<void> {
  const dir = path.join(runDir, "pids");
  if (!existsSync(dir)) return;
  await Promise.all(
    readdirSync(dir).map(async (file) => {
      await stopGroup(Number(file));
      rmSync(path.join(dir, file), { force: true });
    }),
  );
}

/** Kill on exit, synchronously, whatever is still listed: the last resort. */
export function killRunProcessesNow(runDir: string): void {
  const dir = path.join(runDir, "pids");
  if (!existsSync(dir)) return;
  for (const file of readdirSync(dir)) {
    try {
      process.kill(-Number(file), "SIGKILL");
    } catch {
      // already gone
    }
  }
}

/**
 * What an interrupted run left: its processes stopped and its folder removed.
 * A folder whose runner is still alive belongs to a run in progress: refused,
 * two runs would delete each other's Clerk accounts.
 *
 * shortcut: a leftover pid is trusted as ours if its process group exists; a
 * pid reused since by another group leader would be stopped too. Upgrade by
 * recording each process's start time if that ever matters.
 */
export async function cleanLeftoverRuns(): Promise<number> {
  const root = runsRoot();
  let cleaned = 0;
  for (const entry of readdirSync(root)) {
    const runDir = path.join(root, entry);
    const ownerFile = path.join(runDir, "owner");
    const owner = existsSync(ownerFile) ? Number(readFileSync(ownerFile, "utf8")) : 0;
    if (owner > 0 && owner !== process.pid && alive(owner)) {
      throw new Error(
        `Une autre exécution est en cours (processus ${owner}, dossier ${runDir}) : une seule à la fois.`,
      );
    }
    await stopRunProcesses(runDir);
    rmSync(runDir, { recursive: true, force: true });
    cleaned += 1;
  }
  return cleaned;
}

/**
 * Polls `url` until the server answers (below 500; redirects not followed, as
 * Clerk sends a visitor without cookies through its handshake). Fails early
 * when `child` exits.
 */
export async function waitForHttp(
  url: string,
  options: { timeoutMs: number; child?: ChildProcess; runDir?: string; name?: string },
): Promise<void> {
  const deadline = Date.now() + options.timeoutMs;
  let exited: number | null | undefined;
  options.child?.once("exit", (code) => {
    exited = code;
  });
  let last = "aucune réponse";
  while (Date.now() < deadline) {
    if (exited !== undefined) break;
    try {
      const response = await fetch(url, { redirect: "manual", signal: AbortSignal.timeout(5_000) });
      if (response.status < 500) return;
      last = `HTTP ${response.status}`;
    } catch (error) {
      last = (error as Error).name;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  const tail = options.runDir && options.name ? `\n${logTail(options.runDir, options.name)}` : "";
  throw new Error(
    exited !== undefined
      ? `${options.name ?? url} s'est arrêté (code ${exited}) avant d'être prêt.${tail}`
      : `${url} n'a pas répondu en ${options.timeoutMs / 1000} s (dernier essai : ${last}).${tail}`,
  );
}
