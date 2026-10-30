import path from "node:path";
import { harnessRoot, privateDir } from "./processes";

/**
 * Where a run writes what it leaves: E2E_OUTPUT_DIR, by default the folder
 * `sorties` of the harness's own folder (`harnessRoot()`, private to this
 * user). Never the repository: the HTML report, traces of failures and the
 * screenshots of the pages go there.
 */
export function outputRoot(): string {
  return process.env.E2E_OUTPUT_DIR ?? privateDir(path.join(harnessRoot(), "sorties"));
}
