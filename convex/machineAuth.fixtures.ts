import { convexTest } from "convex-test";
import { api } from "./_generated/api";
import schema from "./schema";

export async function machineFixture(
  modules: Record<string, () => Promise<unknown>>,
) {
  const t = convexTest(schema, modules);
  await t.run(async (ctx) => {
    await ctx.db.insert("users", {
      clerkId: "machine-admin",
      role: "admin",
      firstName: "Synthetic",
      lastName: "Admin",
      email: "machine-admin@example.invalid",
      language: "en",
      createdAt: 1_800_000_000_000,
    });
  });
  const admin = t.withIdentity({ subject: "machine-admin" });
  const created = await admin.mutation(api.machines.createMachine, {
    name: "Synthetic authentication machine",
  });
  return { t, admin, ...created };
}

export const machineRoutes = [
  ["POST", "/api/machine/heartbeat"],
  ["GET", "/api/machine/session/poll"],
  ["POST", "/api/machine/session/start"],
  ["POST", "/api/machine/session/end"],
  ["GET", "/api/machine/session/status"],
  ["POST", "/api/machine/data"],
  ["GET", "/api/machine/training/poll"],
  ["GET", "/api/machine/roster"],
  ["POST", "/api/machine/profiles"],
  ["POST", "/api/machine/training/start"],
  ["POST", "/api/machine/training/local"],
  ["POST", "/api/machine/training/end"],
  ["GET", "/api/machine/training/status"],
  ["POST", "/api/machine/training/telemetry"],
] as const;
