import { defineConfig } from "vitest/config";
import {
  assertMeasured,
  CONVEX,
  vitestThresholds,
} from "./scripts/ci/coverage-thresholds.mjs";

// Before any test: every file that must hold the threshold alone is a file
// this configuration measures. Vitest would count the threshold of a name
// that matches nothing as reached; here the command stops instead.
assertMeasured(import.meta.dirname, CONVEX);

export default defineConfig({
  test: {
    environment: "edge-runtime",
    include: ["convex/**/*.test.ts"],
    // Measured only when asked for (`npm run coverage:convex`, which the
    // `convex-tests` job of the CI runs): `npm run test:convex` measures
    // nothing. Every source file counts, loaded by a test or not; the
    // generated bindings, the tests and what only the tests load do not.
    // What is measured, what is left out and the threshold are written once,
    // in scripts/ci/coverage-thresholds.mjs: under 80 % of lines or of
    // branches, on `convex/` as a whole or on one of the three files of the
    // safety chain taken alone, the command fails
    // (docs/framework-de-test.md, "Seuils de couverture de Convex et du site").
    coverage: {
      provider: "v8",
      include: CONVEX.include,
      exclude: CONVEX.exclude,
      thresholds: vitestThresholds(CONVEX),
      reporter: ["text-summary", "json", "html"],
      reportsDirectory: "coverage/convex",
      reportOnFailure: true,
    },
  },
});
