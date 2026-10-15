// The components this backend mounts: the rate limiter (ANH-166), whose
// limits are named in convex/lib/rateLimits.ts, and the e-mail sender of the
// alerts to the gestionnaires (ANH-90, convex/lib/notificationEmail.ts),
// which sends nothing until its three variables are set. Their state lives in
// the components' own tables, out of the application's schema.
//
// Each component is named here, not left to its package's default:
// scripts/ci/convex-generated-api.mjs derives convex/_generated/api.d.ts from
// this file alone, without a deployment, and reads the name from it.
import { defineApp } from "convex/server";
import rateLimiter from "@convex-dev/rate-limiter/convex.config.js";
import resend from "@convex-dev/resend/convex.config.js";

const app = defineApp();
app.use(rateLimiter, { name: "rateLimiter" });
app.use(resend, { name: "resend" });

export default app;
