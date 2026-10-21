// This backend mounts the rate limiter (ANH-166), whose limits are named in
// convex/lib/rateLimits.ts. Alert e-mails use an internal REST action instead
// of a component; their delivery state lives on the application's alerts.
//
// Each component is named here, not left to its package's default:
// scripts/ci/convex-generated-api.mjs derives convex/_generated/api.d.ts from
// this file alone, without a deployment, and reads the name from it.
import { defineApp } from "convex/server";
import rateLimiter from "@convex-dev/rate-limiter/convex.config.js";

const app = defineApp();
app.use(rateLimiter, { name: "rateLimiter" });

export default app;
