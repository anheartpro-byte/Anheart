import { defineConfig } from "vitest/config";

// Unit tests of the dashboard's hooks and components. No browser: hooks run in
// React on a minimal host, components are rendered to static markup. The tests
// of lib/ run with `npm run test:ecg`; the end-to-end browser suite is a
// separate matter (ANH-83).
export default defineConfig({
  resolve: {
    alias: { "@": import.meta.dirname },
  },
  test: {
    environment: "node",
    include: ["hooks/**/*.test.{ts,tsx}", "components/**/*.test.{ts,tsx}"],
    // Measured only when asked for (`npm run coverage:site`, and the quality
    // report of the CI). The same files as in vitest.ecg.config.mts: the two
    // suites test one site, and the report adds their measures up. No
    // threshold (docs/framework-de-test.md, section CI).
    coverage: {
      provider: "v8",
      include: ["lib/**/*.{ts,tsx}", "hooks/**/*.{ts,tsx}", "components/**/*.{ts,tsx}"],
      exclude: ["**/*.test.{ts,tsx}"],
      reporter: ["text-summary", "json", "html"],
      reportsDirectory: "coverage/site-components",
      reportOnFailure: true,
    },
  },
});
