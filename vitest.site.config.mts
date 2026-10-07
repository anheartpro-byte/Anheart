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
  },
});
