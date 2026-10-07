// The coverage the CI requires of Convex and of the site
// (docs/framework-de-test.md, section CI, "Seuils de couverture de Convex et du site").
//
// One place for it. The Vitest configurations at the root of the repository
// enforce what is written here (`npm run coverage:convex` in `convex-tests`,
// `npm run coverage:site` in `web`), and the quality report shows it
// (scripts/ci/quality-report.mjs). Nothing else holds a copy: a folder added
// to a list below is tested, measured and reported from the next run on.
//
// Plain data and a few small functions, nothing to install: Vitest loads this
// file from its configurations, and the `changes` job reads it as it is.

import { readdirSync } from "node:fs";
import { join } from "node:path";

/** The share of lines, and of branches, each measure below must reach: a percentage. */
export const THRESHOLD = 80;

/**
 * Convex: every source file of `convex/` counts, loaded by a test or not.
 *
 * `exclude` lists what is not source. Each entry is named with its reason in
 * the documentation, and a test holds the two together: nothing leaves the
 * measure without a line there.
 *
 * `alone` is the Convex part of the safety chain (what a machine may write,
 * the stop request, the access checks): each of these files must reach the
 * threshold by itself, not only as part of the whole.
 */
export const CONVEX = {
  threshold: THRESHOLD,
  include: ["convex/**/*.ts"],
  exclude: [
    // Bindings Convex generates: not written here.
    "convex/_generated/**",
    // The tests, and what only the tests load.
    "convex/**/*.test.ts",
    "convex/**/*.fixtures.ts",
    "convex/test.setup.ts",
    "convex/authorization.matrix.ts",
  ],
  alone: ["convex/training.ts", "convex/http.ts", "convex/lib/auth.ts"],
};

/**
 * The site: each suite with the folders whose tests it runs
 * (`npm run test:ecg`, `npm run test:site`). Every folder named here is
 * measured: every source file of it, loaded by a test or not. Only the test
 * files themselves are left out.
 *
 * To put another folder of the site under the threshold, add its name to the
 * suite that runs its tests: that is the whole change.
 */
export const SITE = {
  threshold: THRESHOLD,
  suites: {
    ecg: ["lib"],
    site: ["hooks", "components"],
  },
  exclude: ["**/*.test.{ts,tsx}"],
};

/** @returns {string[]} every folder of the site that is measured, in the order of the suites */
export const siteFolders = () => Object.values(SITE.suites).flat();

/** @param {readonly string[]} folders @returns {string[]} the test files of these folders, as Vitest globs */
export const testsOf = (folders) => folders.map((folder) => `${folder}/**/*.test.{ts,tsx}`);

/** @param {readonly string[]} folders @returns {string[]} the files of these folders that are measured */
export const sourcesOf = (folders) => folders.map((folder) => `${folder}/**/*.{ts,tsx}`);

/**
 * The `coverage.thresholds` of a Vitest configuration: lines and branches of
 * the whole measure, and of each file that must hold alone. Vitest ends with
 * a failure, naming the measure and the figure, when one is not reached.
 * @param {{threshold: number, alone?: readonly string[]}} measure
 * @returns {Record<string, number | {lines: number, branches: number}>}
 */
export function vitestThresholds({ threshold, alone = [] }) {
  const required = { lines: threshold, branches: threshold };
  return { ...required, ...Object.fromEntries(alone.map((file) => [file, { ...required }])) };
}

// --- What a threshold judges must be there to be judged ----------------------------
//
// Vitest counts as reached the threshold of a name that matches no measured
// file, and a folder that holds no file leaves the measure without a word. A
// file of the safety chain renamed, or a folder mistyped in a list above,
// would then leave its gate green. `assertMeasured` is called by the Vitest
// configurations themselves: the command that enforces a threshold refuses to
// start when something that threshold names is not measured.

/**
 * A glob of the lists above as a regular expression: `**` is any depth of
 * folders, `*` anything within one folder, `{a,b}` one of the alternatives.
 * @param {string} glob @returns {RegExp}
 */
export function globToRegExp(glob) {
  const literal = (/** @type {string} */ text) => text.replaceAll(/[.+?^$()|[\]\\]/g, "\\$&");
  let pattern = "";
  for (let at = 0; at < glob.length; ) {
    if (glob.startsWith("**/", at)) {
      pattern += "(?:[^/]+/)*";
      at += 3;
    } else if (glob.startsWith("**", at)) {
      pattern += ".*";
      at += 2;
    } else if (glob[at] === "*") {
      pattern += "[^/]*";
      at += 1;
    } else if (glob[at] === "{" && glob.includes("}", at)) {
      const end = glob.indexOf("}", at);
      pattern += `(?:${glob
        .slice(at + 1, end)
        .split(",")
        .map(literal)
        .join("|")})`;
      at = end + 1;
    } else {
      pattern += literal(glob[at] ?? "");
      at += 1;
    }
  }
  return new RegExp(`^${pattern}$`);
}

/**
 * Every file under a folder, as paths from the root written with `/`. A
 * folder that is not there holds none. Installed dependencies and hidden
 * folders are not walked.
 * @param {string} root @param {string} folder relative to the root, "" for the root itself
 * @returns {string[]}
 */
function filesUnder(root, folder) {
  /** @type {import("node:fs").Dirent[]} */
  let entries;
  try {
    entries = readdirSync(join(root, folder), { withFileTypes: true });
  } catch {
    return [];
  }
  return entries.flatMap((entry) => {
    const path = folder === "" ? entry.name : `${folder}/${entry.name}`;
    if (!entry.isDirectory()) return [path];
    return entry.name === "node_modules" || entry.name.startsWith(".") ? [] : filesUnder(root, path);
  });
}

/**
 * The files one include glob of a measure counts: those it matches, less the exclusions.
 * @param {string} root @param {string} glob @param {readonly string[]} exclude @returns {string[]}
 */
function measuredBy(root, glob, exclude) {
  // The folders written before the first wildcard: only they are walked.
  const fixed = glob.slice(0, glob.search(/[*{]|$/));
  const folder = fixed.slice(0, Math.max(fixed.lastIndexOf("/"), 0));
  const [included, excluded] = [globToRegExp(glob), exclude.map(globToRegExp)];
  return filesUnder(root, folder).filter((file) => included.test(file) && !excluded.some((left) => left.test(file)));
}

/**
 * The files a measure counts, from the same lists its Vitest configuration is given.
 * @param {string} root the root of the repository
 * @param {{include: readonly string[], exclude?: readonly string[]}} measure
 * @returns {string[]} paths from the root, sorted, each once
 */
export function measuredFiles(root, { include, exclude = [] }) {
  return [...new Set(include.flatMap((glob) => measuredBy(root, glob, exclude)))].sort();
}

/**
 * What a measure names without measuring it: an include glob that matches no
 * measured file, a file that must hold the threshold alone and is not measured.
 * @param {string} root the root of the repository
 * @param {{include: readonly string[], exclude?: readonly string[], alone?: readonly string[]}} measure
 * @returns {string[]} one sentence per problem; empty when everything named is measured
 */
export function unjudged(root, { include, exclude = [], alone = [] }) {
  const measured = measuredFiles(root, { include, exclude });
  return [
    ...include
      .filter((glob) => measuredBy(root, glob, exclude).length === 0)
      .map((glob) => `"${glob}" matches no measured file: nothing of it would be judged`),
    ...alone
      .filter((file) => !measured.includes(file))
      .map(
        (file) =>
          `"${file}" must reach the threshold alone but is not a measured file ` +
          "(renamed, removed or excluded?): its threshold would count as reached",
      ),
  ];
}

/**
 * Stops the command when a measure names something it does not measure.
 * @param {string} root the root of the repository
 * @param {{include: readonly string[], exclude?: readonly string[], alone?: readonly string[]}} measure
 */
export function assertMeasured(root, measure) {
  const problems = unjudged(root, measure);
  if (problems.length === 0) return;
  throw new Error(
    `Coverage threshold without an object (scripts/ci/coverage-thresholds.mjs):\n- ${problems.join("\n- ")}\n` +
      "Update the list, or put the file back: a threshold that judges nothing must not pass.",
  );
}

/**
 * Whether a measure reaches a threshold, on its lines and on its branches. A
 * measure with nothing to cover holds; a share is never rounded up.
 * @param {{lines: {covered: number, total: number}, branches: {covered: number, total: number}}} numbers
 * @param {number} threshold a percentage
 */
export function holds({ lines, branches }, threshold) {
  const reached = (/** @type {{covered: number, total: number}} */ { covered, total }) =>
    covered * 100 >= threshold * total;
  return reached(lines) && reached(branches);
}
