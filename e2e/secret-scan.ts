import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { knownSecrets } from "./harness/secrets";

/** A signed token (Clerk session, Convex authentication): three base64url parts. */
const JWT = /eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g;
/** Clerk's development browser, which stands for the signed-in browser. */
const DEV_BROWSER = /dvb_[A-Za-z0-9]+/g;
const MASK = "[masque]";

/** Every file under this folder. */
function* files(folder: string): Generator<string> {
  if (!existsSync(folder)) return;
  for (const entry of readdirSync(folder)) {
    const full = path.join(folder, entry);
    if (statSync(full).isDirectory()) yield* files(full);
    else yield full;
  }
}

function inTemporaryFolder<T>(work: (folder: string) => T): T {
  const folder = mkdtempSync(path.join(os.tmpdir(), "anheart-e2e-scan-"));
  try {
    return work(folder);
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}

/**
 * Rewrites a kept trace without what signs anyone in: the run's secret values,
 * signed tokens, development browser ids, the Authorization, Cookie,
 * Set-Cookie and Clerk headers, the Clerk cookies (in the requests and in the
 * browser state the trace records) and the `__clerk_*` URL parameters. Text is read byte for byte (latin1), so
 * that images and other binaries come back unchanged.
 */
function redactTrace(zip: string): void {
  inTemporaryFolder((folder) => {
    const tree = path.join(folder, "trace");
    execFileSync("unzip", ["-q", zip, "-d", tree]);
    for (const file of files(tree)) {
      const before = readFileSync(file, "latin1");
      let after = before
        .replace(JWT, MASK)
        .replace(DEV_BROWSER, MASK)
        .replace(/("name":\s*"(?:authorization|cookie|set-cookie|[^"]*clerk[^"]*)",\s*"value":\s*)"(?:[^"\\]|\\.)*"/gi, `$1"${MASK}"`)
        .replace(/("name":\s*"__(?:session|client|clerk)[^"]*",\s*"value":\s*)"(?:[^"\\]|\\.)*"/g, `$1"${MASK}"`)
        .replace(/(__clerk_[a-z_]+=)[^&"\s\\]+/g, `$1${MASK}`);
      for (const value of knownSecrets()) after = after.split(value).join(MASK);
      if (after !== before) writeFileSync(file, after, "latin1");
    }
    const rewritten = path.join(folder, "trace.zip");
    execFileSync("zip", ["-q", "-r", "-X", rewritten, "."], { cwd: tree });
    renameSync(rewritten, zip);
  });
}

function unzipped(zip: string): Buffer {
  return execFileSync("unzip", ["-p", zip], { maxBuffer: 1 << 30 });
}

/** A file, and what a trace archive or the HTML report (a zip in base64 inside index.html) carries. */
function contents(file: string): Buffer[] {
  const raw = readFileSync(file);
  if (file.endsWith(".zip")) return [raw, unzipped(file)];
  const embedded = /data:application\/zip;base64,([A-Za-z0-9+/=]+)/.exec(
    file.endsWith(".html") ? raw.toString("latin1") : "",
  );
  if (!embedded) return [raw];
  return inTemporaryFolder((folder) => {
    const zip = path.join(folder, "report.zip");
    writeFileSync(zip, Buffer.from(embedded[1], "base64"));
    return [raw, unzipped(zip)];
  });
}

function occurrences(haystack: Buffer, needle: Buffer): number {
  let count = 0;
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + 1)) count += 1;
  return count;
}

/**
 * Last reporter of the run. Each kept trace is masked as soon as its test
 * ends, before the HTML report copies it. At the end, counts the run's secret
 * values (held in the runner's memory, never written) and the signed tokens
 * in what the run leaves: traces, captures, the HTML report, the built site.
 * Prints counts only; any occurrence fails the run.
 */
export default class SecretScan implements Reporter {
  constructor(private readonly options: { output: string; folders: string[] }) {}

  printsToStdio(): boolean {
    return true;
  }

  onTestEnd(_test: TestCase, result: TestResult): void {
    for (const attachment of result.attachments) {
      if (attachment.name === "trace" && attachment.path && existsSync(attachment.path)) {
        redactTrace(attachment.path);
      }
    }
  }

  async onEnd(): Promise<{ status?: FullResult["status"] } | void> {
    const values = knownSecrets().map((value) => Buffer.from(value));
    if (values.length === 0) {
      console.log("[e2e] recherche des secrets : aucune valeur connue de l'exécution, recherche impossible");
      return { status: "failed" };
    }
    let scanned = 0;
    let found = 0;
    let tokens = 0;
    for (const folder of this.options.folders) {
      for (const file of files(folder)) {
        scanned += 1;
        for (const buffer of contents(file)) {
          for (const value of values) found += occurrences(buffer, value);
          const text = buffer.toString("latin1");
          tokens += (text.match(JWT)?.length ?? 0) + (text.match(DEV_BROWSER)?.length ?? 0);
        }
      }
    }
    console.log(
      `[e2e] recherche des secrets : ${values.length} valeurs de l'exécution, ${scanned} fichiers, ${found} occurrence(s), ${tokens} jeton(s) signé(s)`,
    );
    console.log(`[e2e] rapport, traces et captures : ${this.options.output}`);
    if (found > 0 || tokens > 0) return { status: "failed" };
  }
}
