import { defineConfig } from "vitest/config";
import { SITE, testsOf } from "./scripts/ci/coverage-thresholds.mjs";

// The site's unit tests of `lib/`, run by `npm run test:lib` (called
// `test:ecg` until ANH-183: the name dated from the ECG library these tests
// first covered). Which folders this suite runs is written in
// scripts/ci/coverage-thresholds.mjs. Nothing is measured here: the coverage
// of the site is one run of all its tests, `npm run coverage:site`
// (vitest.site-coverage.config.mts).
export default defineConfig({
  test: {
    environment: "node",
    include: testsOf(SITE.suites.lib),
  },
});
