import { defineConfig } from "vitest/config";
import { SITE, testsOf } from "./scripts/ci/coverage-thresholds.mjs";

// Unit tests of the dashboard's hooks, components and pages, run by
// `npm run test:site`. No browser: hooks and components run in React on the
// minimal document of test-support/, or are rendered to static markup. The
// pages under app/ are rendered in jsdom, which each of their test files asks
// for by itself (`// @vitest-environment jsdom`): the suite stays on node for
// everything else. The tests of lib/ run with `npm run test:ecg`; the
// end-to-end browser suite is a separate matter (ANH-83).
//
// Which folders this suite runs is written in
// scripts/ci/coverage-thresholds.mjs, with the threshold their coverage must
// reach. Nothing is measured here: the coverage of the site is one run of all
// its tests, `npm run coverage:site` (vitest.site-coverage.config.mts).
export default defineConfig({
  resolve: {
    alias: { "@": import.meta.dirname },
  },
  test: {
    environment: "node",
    include: testsOf(SITE.suites.site),
  },
});
