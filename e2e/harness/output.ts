import os from "node:os";
import path from "node:path";

/**
 * Where a run writes what it leaves: E2E_OUTPUT_DIR, by default one folder of
 * the system's temporary folder. Never the repository: the HTML report,
 * traces of failures and the screenshots of the pages go there.
 */
export function outputRoot(): string {
  return process.env.E2E_OUTPUT_DIR ?? path.join(os.tmpdir(), "anheart-e2e", "sorties");
}
