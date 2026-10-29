import { execFile } from "node:child_process";
import { createHmac, randomBytes, randomUUID } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";
import { freePort, runToEnd, startProcess, waitForHttp } from "./processes";
import { assertLocal, childEnv, redact, remember } from "./secrets";

/** Official local backend, pinned: the same build on every run. */
export const BACKEND_VERSION = "precompiled-2026-10-10-72ce13c";

const TARGETS: Record<string, string> = {
  "darwin-arm64": "aarch64-apple-darwin",
  "darwin-x64": "x86_64-apple-darwin",
  "linux-arm64": "aarch64-unknown-linux-gnu",
  "linux-x64": "x86_64-unknown-linux-gnu",
};

export type Backend = {
  /** Client API of the deployment (what the site's NEXT_PUBLIC_CONVEX_URL is). */
  url: string;
  /** HTTP actions (the machine routes, the Clerk webhook). */
  siteUrl: string;
  adminKey: string;
};

/**
 * The binary in the cache the Convex CLI uses itself
 * (`~/.cache/convex/binaries/<version>/`), downloaded once.
 */
async function backendBinary(): Promise<string> {
  const target = TARGETS[`${process.platform}-${process.arch}`];
  if (!target) throw new Error(`Plateforme sans backend Convex local : ${process.platform}-${process.arch}`);
  const dir = path.join(os.homedir(), ".cache", "convex", "binaries", BACKEND_VERSION);
  const binary = path.join(dir, "convex-local-backend");
  if (existsSync(binary)) return binary;
  const url = `https://github.com/get-convex/convex-backend/releases/download/${BACKEND_VERSION}/convex-local-backend-${target}.zip`;
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Téléchargement du backend Convex : HTTP ${response.status} (${url})`);
  const staging = `${dir}.part-${process.pid}`;
  mkdirSync(staging, { recursive: true });
  try {
    const zip = path.join(staging, "backend.zip");
    writeFileSync(zip, Buffer.from(await response.arrayBuffer()));
    await promisify(execFile)("unzip", ["-o", "-q", zip, "-d", staging]);
    chmodSync(path.join(staging, "convex-local-backend"), 0o755);
    mkdirSync(dir, { recursive: true });
    renameSync(path.join(staging, "convex-local-backend"), binary);
  } finally {
    rmSync(staging, { recursive: true, force: true });
  }
  return binary;
}

/** A fresh backend on the loopback, its database in the run folder. */
export async function startBackend(runDir: string, runId: string): Promise<Backend> {
  const binary = await backendBinary();
  const port = await freePort();
  const sitePort = await freePort();
  const instanceName = `anheart-e2e-${runId}`;
  const instanceSecret = remember(randomBytes(32).toString("hex"));
  const { stdout } = await promisify(execFile)(binary, [
    "keygen",
    "admin-key",
    "--instance-name",
    instanceName,
    "--instance-secret",
    instanceSecret,
  ]);
  const adminKey = remember(stdout.trim());
  const data = path.join(runDir, "convex");
  mkdirSync(data, { recursive: true });
  const child = startProcess(
    runDir,
    "convex-backend",
    binary,
    [
      "--interface",
      "127.0.0.1",
      "--port",
      String(port),
      "--site-proxy-port",
      String(sitePort),
      "--instance-name",
      instanceName,
      "--instance-secret",
      instanceSecret,
      "--local-storage",
      path.join(data, "storage"),
      "--disable-beacon",
      path.join(data, "backend.sqlite3"),
    ],
    { cwd: data, env: childEnv({}) },
  );
  const url = assertLocal(`http://127.0.0.1:${port}`);
  const siteUrl = assertLocal(`http://127.0.0.1:${sitePort}`);
  await waitForHttp(`${url}/instance_name`, {
    timeoutMs: 60_000,
    child,
    runDir,
    name: "convex-backend",
  });
  return { url, siteUrl, adminKey };
}

/** Deployment variables, by the backend's own API: no value on a command line. */
export async function setDeploymentEnv(
  backend: Backend,
  values: Record<string, string>,
): Promise<void> {
  const response = await fetch(`${backend.url}/api/update_environment_variables`, {
    method: "POST",
    headers: {
      authorization: `Convex ${backend.adminKey}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({
      changes: Object.entries(values).map(([name, value]) => ({ name, value })),
    }),
  });
  if (!response.ok) {
    throw new Error(`Variables du backend local : HTTP ${response.status} ${redact(await response.text())}`);
  }
}

/**
 * Pushes `convex/` with the CLI. The deployment is chosen by `--env-file`
 * alone (no `.env.local`, no inherited CONVEX_ variable), and neither
 * `convex/_generated` nor anything else of the tree is written.
 */
export async function pushFunctions(
  runDir: string,
  repoRoot: string,
  backend: Backend,
): Promise<void> {
  const envFile = path.join(runDir, "convex.env");
  writeFileSync(
    envFile,
    `CONVEX_SELF_HOSTED_URL=${backend.url}\nCONVEX_SELF_HOSTED_ADMIN_KEY=${backend.adminKey}\n`,
    { mode: 0o600 },
  );
  try {
    await runToEnd(
      runDir,
      "convex-deploy",
      path.join(repoRoot, "node_modules", ".bin", "convex"),
      ["deploy", "--env-file", envFile, "--codegen", "disable", "--typecheck", "disable", "--yes"],
      { cwd: repoRoot, env: childEnv({}) },
    );
  } finally {
    rmSync(envFile, { force: true });
  }
}

/**
 * A Clerk webhook event, signed as Svix signs it, sent to the backend's own
 * route: the mirror the dev instance's webhook would write.
 */
export async function replayClerkEvent(
  backend: Backend,
  signingSecret: string,
  type: string,
  data: unknown,
): Promise<void> {
  const id = `msg_e2e_${randomUUID()}`;
  const stamp = String(Math.floor(Date.now() / 1000));
  const body = JSON.stringify({ object: "event", type, timestamp: Date.now(), data });
  const key = Buffer.from(signingSecret.slice("whsec_".length), "base64");
  const signature = createHmac("sha256", key).update(`${id}.${stamp}.${body}`).digest("base64");
  const response = await fetch(`${backend.siteUrl}/clerk/webhook`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "svix-id": id,
      "svix-timestamp": stamp,
      "svix-signature": `v1,${signature}`,
    },
    body,
  });
  if (response.status !== 204) {
    throw new Error(`Webhook Clerk rejoué (${type}) : HTTP ${response.status} ${await response.text()}`);
  }
}

export function newWebhookSecret(): string {
  return remember(`whsec_${randomBytes(24).toString("base64")}`);
}

/** The verified identity a Clerk token of this account would carry. */
export type ActingIdentity = {
  subject: string;
  issuer: string;
  org_id: string;
  org_role: string;
  email: string;
  email_verified: boolean;
};

/**
 * A function called with the admin key: as that account, or as the deployment's
 * operator (`null`, as the CLI runs an internal function).
 */
export async function callAs<T>(
  backend: Backend,
  identity: ActingIdentity | null,
  kind: "query" | "mutation" | "action",
  functionPath: string,
  args: Record<string, unknown>,
): Promise<T> {
  const acting = identity ? `:${Buffer.from(JSON.stringify(identity)).toString("base64")}` : "";
  const response = await fetch(`${backend.url}/api/${kind}`, {
    method: "POST",
    headers: {
      authorization: `Convex ${backend.adminKey}${acting}`,
      "content-type": "application/json",
    },
    body: JSON.stringify({ path: functionPath, format: "convex_encoded_json", args: [args] }),
  });
  const result = (await response.json()) as
    | { status: "success"; value: T }
    | { status: "error"; errorMessage: string };
  if (result.status !== "success") {
    throw new Error(`${functionPath} : ${redact(result.errorMessage)}`);
  }
  return result.value;
}

/**
 * The signed medical file the console carries, registered as a deployment is
 * bootstrapped (docs/convex.md, `medicalParameters:registerVersion`): without
 * it the site refuses every launch.
 */
export async function registerMedicalParameters(backend: Backend, repoRoot: string): Promise<string> {
  const config = path.join(repoRoot, "raspberry-pi", "config");
  return await callAs<string>(backend, null, "action", "medicalParameters:registerVersion", {
    content: readFileSync(path.join(config, "medical_parameters.json"), "utf8"),
    signatureBase64: readFileSync(path.join(config, "medical_parameters.sig")).toString("base64"),
  });
}
