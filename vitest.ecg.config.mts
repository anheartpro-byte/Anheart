import { defineConfig } from "vitest/config";

// The site's unit tests, run by `npm run test:ecg` (the name dates from the
// ECG library these tests used to cover; CI calls it by that name).
export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/**/*.test.ts"],
  },
});
