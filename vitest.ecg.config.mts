import { defineConfig } from "vitest/config";

// The site's unit tests, run by `npm run test:ecg` (the name dates from the
// ECG library these tests used to cover; CI calls it by that name).
export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts"],
    // Measured only when asked for (`npm run coverage:ecg`, and the quality
    // report of the CI). The same files as in vitest.site.config.mts: the two
    // suites test one site, and the report adds their measures up. No
    // threshold (docs/framework-de-test.md, section CI).
    coverage: {
      provider: "v8",
      include: ["lib/**/*.{ts,tsx}", "hooks/**/*.{ts,tsx}", "components/**/*.{ts,tsx}"],
      exclude: ["**/*.test.{ts,tsx}"],
      reporter: ["text-summary", "json", "html"],
      reportsDirectory: "coverage/site-lib",
      reportOnFailure: true,
    },
  },
});
