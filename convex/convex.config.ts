// The components this backend mounts (ANH-166): the rate limiter, whose
// limits are named in convex/lib/rateLimits.ts. Its state lives in the
// component's own table, out of the application's schema.
//
// Each component is named here, not left to its package's default:
// scripts/ci/convex-generated-api.mjs derives convex/_generated/api.d.ts from
// this file alone, without a deployment, and reads the name from it.
import { defineApp } from "convex/server";
import rateLimiter from "@convex-dev/rate-limiter/convex.config.js";

const app = defineApp();
app.use(rateLimiter, { name: "rateLimiter" });

export default app;
