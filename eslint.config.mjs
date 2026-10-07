import { defineConfig, globalIgnores } from "eslint/config";
import nextCoreWebVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";
import convexPlugin from "@convex-dev/eslint-plugin";

export default defineConfig([
  ...nextCoreWebVitals,
  ...nextTypescript,
  ...convexPlugin.configs.recommended,
  globalIgnores([
    "convex/_generated/**",
    "**/.venv/**",
    "**/.venv-*/**",
    "**/venv/**",
    ".next/**",
    "out/**",
    "build/**",
    "simulation/out/**",
    "coverage/**",
    "next-env.d.ts",
  ]),
]);
