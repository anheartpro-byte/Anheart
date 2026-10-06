import { defineConfig } from "vitest/config";

// Unit tests of the dashboard's own code (hooks, components, lib/training).
// No browser: hooks run in React on a minimal host, components are rendered to
// static markup. The end-to-end browser suite is a separate matter (ANH-83).
export default defineConfig({
  resolve: {
    alias: { "@": import.meta.dirname },
  },
  test: {
    environment: "node",
    include: [
      "hooks/**/*.test.{ts,tsx}",
      "components/**/*.test.{ts,tsx}",
      "lib/training.test.ts",
    ],
  },
});
