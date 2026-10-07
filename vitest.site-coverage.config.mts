import { defineConfig } from "vitest/config";
import {
  assertMeasured,
  SITE,
  siteFolders,
  sourcesOf,
  testsOf,
  vitestThresholds,
} from "./scripts/ci/coverage-thresholds.mjs";

/** What this run measures: every source file of the measured folders, the tests left out. */
const measure = { include: sourcesOf(siteFolders()), exclude: SITE.exclude };

// Before any test: every measured folder holds files to measure. A folder
// renamed or mistyped in the list would otherwise leave the measure without a
// word, and the threshold would judge what is left.
assertMeasured(import.meta.dirname, measure);

// The coverage of the site, measured and held to its threshold:
// `npm run coverage:site`, which the `web` job of the CI runs.
//
// One run of every test of the site (those of `npm run test:ecg` and those of
// `npm run test:site` together), so that the figure the threshold judges is
// the one a developer reads at the end of the same command. Every source file
// of the measured folders counts, loaded by a test or not; only the test
// files are left out. Under 80 % of lines or of branches the command fails.
// The folders and the threshold are written once, in
// scripts/ci/coverage-thresholds.mjs
// (docs/framework-de-test.md, "Seuils de couverture de Convex et du site").
export default defineConfig({
  resolve: {
    alias: { "@": import.meta.dirname },
  },
  test: {
    environment: "node",
    include: testsOf(siteFolders()),
    coverage: {
      provider: "v8",
      include: measure.include,
      exclude: measure.exclude,
      thresholds: vitestThresholds(SITE),
      reporter: ["text-summary", "json", "html"],
      reportsDirectory: "coverage/site",
      reportOnFailure: true,
    },
  },
});
