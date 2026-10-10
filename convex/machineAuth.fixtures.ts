import { convexTest } from "convex-test";
import { api, internal } from "./_generated/api";
import schema from "./schema";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";

/**
 * An admin account written before organisations existed, on a deployment where
 * Clerk Organizations is not configured (`ANHEART_ORG_ID` unset), after the
 * multi-organisation migration (ANH-114) has run once.
 */
export async function machineFixture(
  modules: Record<string, () => Promise<unknown>>,
) {
  delete process.env.ANHEART_ORG_ID;
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
  await t.mutation(internal.migrations.multiOrganization.attachExistingRowsToAnheart, {});
  const admin = t.withIdentity({ subject: "machine-admin" });
  const created = await admin.mutation(api.machines.createMachine, {
    name: "Synthetic authentication machine",
  });
  return { t, admin, ...created };
}

/** What an accepted machine request carries: its key and the contract it speaks. */
export function machineHeaders(apiKey: string): Record<string, string> {
  return {
    Authorization: `Bearer ${apiKey}`,
    [CONTRACT_HEADER]: CONTRACT_VERSION,
  };
}

export const machineRoutes = [
  ["POST", "/api/machine/heartbeat"],
  ["GET", "/api/machine/training/poll"],
  ["GET", "/api/machine/roster"],
  ["POST", "/api/machine/profiles"],
  ["POST", "/api/machine/training/start"],
  ["POST", "/api/machine/training/local"],
  ["POST", "/api/machine/training/end"],
  ["GET", "/api/machine/training/status"],
  ["POST", "/api/machine/training/telemetry"],
  ["POST", "/api/machine/training/events"],
] as const;
