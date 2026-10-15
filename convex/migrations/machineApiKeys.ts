/**
 * Data migration of ANH-165: the single key of each machine
 * (`machines.apiKey`, `machines.apiKeySelector`) moves into the table
 * `machine_api_keys`, where keys are rotated, revoked and expire.
 *
 * One internal mutation, run by hand once per deployment after this code is
 * deployed (never from the application, never by a schedule):
 *
 *     npx convex run migrations/machineApiKeys:moveMachineKeysToTable
 *
 * Nothing is interrupted, before or after it: until it has run, a key still
 * in `machines` opens the machine routes as before (convex/lib/machineAuth.ts);
 * after it, the same key opens them as a row of the table. Idempotent: a
 * second run finds no key left in `machines` and says so in its result.
 *
 * A value that is not a verifier with its selector (the reversible encodings
 * refused since ANH-121) opens nothing: it is cleared, not moved, and counted
 * in `legacyDiscarded`. Such a machine needs a new key
 * (`machineKeys.createMachineKey`).
 */
import { v } from "convex/values";
import { internalMutation } from "../_generated/server";
import { moveLegacyKey } from "../lib/machineKeys";

export const moveMachineKeysToTable = internalMutation({
  args: {},
  returns: v.object({
    machinesChecked: v.number(),
    keysMoved: v.number(),
    legacyDiscarded: v.number(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const machines = await ctx.db.query("machines").collect();
    let keysMoved = 0;
    let legacyDiscarded = 0;
    for (const machine of machines) {
      const outcome = await moveLegacyKey(ctx, machine, now);
      if (outcome === "moved") keysMoved += 1;
      if (outcome === "discarded") legacyDiscarded += 1;
    }
    return { machinesChecked: machines.length, keysMoved, legacyDiscarded };
  },
});
