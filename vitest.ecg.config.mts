import { defineConfig } from "vitest/config";
import { SITE, testsOf } from "./scripts/ci/coverage-thresholds.mjs";

// The site's unit tests of `lib/`, run by `npm run test:ecg` (the name dates
// from the ECG library these tests used to cover; CI calls it by that name).
// Nothing is measured here: the coverage of the site is one run of all its
// tests, `npm run coverage:site` (vitest.site-coverage.config.mts).
export default defineConfig({
  test: {
    environment: "node",
    include: testsOf(SITE.suites.ecg),
  },
});
