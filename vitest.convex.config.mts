import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "edge-runtime",
    include: ["convex/**/*.test.ts"],
    // Measured only when asked for (`npm run coverage:convex`, and the quality
    // report of the CI): `npm run test:convex` measures nothing. Every source
    // file counts, loaded by a test or not; the generated bindings, the tests
    // and what only the tests load do not. No threshold: read, not enforced
    // (docs/framework-de-test.md, section CI).
    coverage: {
      provider: "v8",
      include: ["convex/**/*.ts"],
      exclude: [
        "convex/_generated/**",
        "convex/**/*.test.ts",
        "convex/**/*.fixtures.ts",
        "convex/test.setup.ts",
        "convex/authorization.matrix.ts",
      ],
      reporter: ["text-summary", "json", "html"],
      reportsDirectory: "coverage/convex",
      reportOnFailure: true,
    },
  },
});
