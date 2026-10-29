import type { FullResult, Reporter } from "@playwright/test/reporter";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { knownSecrets } from "./harness/secrets";

/** Every file under these folders, a trace or report archive opened. */
function* files(folder: string): Generator<string> {
  if (!existsSync(folder)) return;
  for (const entry of readdirSync(folder)) {
    const full = path.join(folder, entry);
    if (statSync(full).isDirectory()) yield* files(full);
    else yield full;
  }
}

function unzipped(zip: string): Buffer {
  return execFileSync("unzip", ["-p", zip], { maxBuffer: 1 << 30 });
}

function contents(file: string): Buffer[] {
  const raw = readFileSync(file);
  if (file.endsWith(".zip")) return [raw, unzipped(file)];
  // The HTML report carries its data as a zip in base64 inside index.html.
  const embedded = /data:application\/zip;base64,([A-Za-z0-9+/=]+)/.exec(
    file.endsWith(".html") ? raw.toString("latin1") : "",
  );
  if (!embedded) return [raw];
  const folder = mkdtempSync(path.join(os.tmpdir(), "anheart-e2e-scan-"));
  try {
    const zip = path.join(folder, "report.zip");
    writeFileSync(zip, Buffer.from(embedded[1], "base64"));
    return [raw, unzipped(zip)];
  } finally {
    rmSync(folder, { recursive: true, force: true });
  }
}

function occurrences(haystack: Buffer, needle: Buffer): number {
  let count = 0;
  for (let at = haystack.indexOf(needle); at !== -1; at = haystack.indexOf(needle, at + 1)) count += 1;
  return count;
}

/**
 * Last reporter of the run: counts the secret values of this run (held in
 * memory by the runner, never written) in what the run leaves, the traces,
 * captures, the HTML report and the built site. Prints the count only; any
 * occurrence fails the run.
 */
export default class SecretScan implements Reporter {
  constructor(private readonly options: { folders: string[] }) {}

  printsToStdio(): boolean {
    return true;
  }

  async onEnd(): Promise<{ status?: FullResult["status"] } | void> {
    const values = knownSecrets().map((value) => Buffer.from(value));
    if (values.length === 0) {
      console.log("[e2e] recherche des secrets : aucune valeur connue de l'exécution, recherche impossible");
      return { status: "failed" };
    }
    let scanned = 0;
    let found = 0;
    for (const folder of this.options.folders) {
      for (const file of files(folder)) {
        scanned += 1;
        for (const buffer of contents(file)) {
          for (const value of values) found += occurrences(buffer, value);
        }
      }
    }
    console.log(
      `[e2e] recherche des secrets : ${values.length} valeurs de l'exécution, ${scanned} fichiers (${this.options.folders
        .map((folder) => path.relative(process.cwd(), folder))
        .join(", ")}), ${found} occurrence(s)`,
    );
    if (found > 0) return { status: "failed" };
  }
}
