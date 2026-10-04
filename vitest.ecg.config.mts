import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "node",
    include: ["lib/ecg.test.ts", "lib/ecg/**/*.test.ts"],
  },
});
