// The coverage the CI requires of Convex and of the site
// (docs/framework-de-test.md, section CI, "Seuils de couverture de Convex et du site").
//
// One place for it. The Vitest configurations at the root of the repository
// enforce what is written here (`npm run coverage:convex` in `convex-tests`,
// `npm run coverage:site` in `web`), and the quality report shows it
// (scripts/ci/quality-report.mjs). Nothing else holds a copy: a folder added
// to a list below is tested, measured and reported from the next run on.
//
// Plain data and a few small functions, no import: Vitest loads this file
// from its configurations, and the `changes` job reads it without any install.

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
