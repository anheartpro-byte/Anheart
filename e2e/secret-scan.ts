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

/** Written in the output folder when nothing of the run may be sent anywhere. */
export const DO_NOT_SEND = "ne-pas-envoyer";

/**
 * Last reporter of the run. As soon as a test ends, before the HTML report
 * copies its attachments (at the end of the run): its error context is
 * deleted, file and attachment, and each kept trace is masked. At the end,
 * counts the run's secret values (held in the runner's memory, never written)
 * and the signed tokens in test-results, the HTML report and the built site;
 * the screenshots are masked and read by a person, not counted. Prints counts
 * only. Any occurrence, or a count it could not make, fails the run closed:
 * test-results and the HTML report are deleted (Playwright then writes back
 * test-results/.last-run.json alone: the status and the ids of failed tests),
 * and the marker DO_NOT_SEND is written in the output folder for the steps
 * that would upload them.
 */
export default class SecretScan implements Reporter {
  constructor(private readonly options: { output: string; folders: string[]; withheld: string[] }) {}

  printsToStdio(): boolean {
    return true;
  }

  onBegin(): void {
    rmSync(path.join(this.options.output, DO_NOT_SEND), { force: true });
  }

  onTestEnd(_test: TestCase, result: TestResult): void {
    // The error context of a failure (error-context.md) holds the page's
    // accessibility snapshot, that is the text the page showed: a health value,
    // a key shown once. The error message and its stack stay in the report.
    for (let index = result.attachments.length - 1; index >= 0; index -= 1) {
      const attachment = result.attachments[index];
      if (attachment.name !== "error-context") continue;
      if (attachment.path) rmSync(attachment.path, { force: true });
      result.attachments.splice(index, 1);
    }
    for (const attachment of result.attachments) {
      if (attachment.name === "trace" && attachment.path && existsSync(attachment.path)) {
        redactTrace(attachment.path);
      }
    }
  }

  /** Nothing of the run leaves: what was counted is deleted, the marker written. */
  private failClosed(reason: string): { status: FullResult["status"] } {
    for (const folder of this.options.withheld) rmSync(folder, { recursive: true, force: true });
    const marker = path.join(this.options.output, DO_NOT_SEND);
    writeFileSync(marker, `${reason}\n`);
    console.log(`[e2e] ${reason} : test-results et rapport supprimés, marqueur ${marker}`);
    return { status: "failed" };
  }

  async onEnd(): Promise<{ status?: FullResult["status"] } | void> {
    const values = knownSecrets().map((value) => Buffer.from(value));
    if (values.length === 0) {
      console.log("[e2e] recherche des secrets : aucune valeur connue de l'exécution, recherche impossible");
      return this.failClosed("recherche des secrets impossible");
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
    if (found > 0 || tokens > 0) return this.failClosed("secret ou jeton signé trouvé");
    console.log(`[e2e] rapport, traces et captures : ${this.options.output}`);
  }
}
