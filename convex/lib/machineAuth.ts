import { v } from "convex/values";
import type { Doc } from "../_generated/dataModel";
import type { QueryCtx } from "../_generated/server";
import { apiKeySelector, verifyApiKey } from "./crypto";
import { keyIsValid } from "./machineKeys";

export const authenticatedMachine = v.union(
  v.object({
    _id: v.id("machines"),
    name: v.string(),
    status: v.string(),
    // Raw dates let the action judge time even when this query is cached.
    // Absent for a key not moved into the table yet. Authenticating never writes.
    key: v.optional(
      v.object({
        id: v.id("machine_api_keys"),
        expiresAt: v.optional(v.number()),
        lastUsedAt: v.optional(v.number()),
      }),
    ),
  }),
  v.null(),
);

/** Whether the machine may authenticate at all: it exists, is not deleted, nor disabled. */
function mayAuthenticate(
  machine: Doc<"machines"> | null,
): machine is Doc<"machines"> {
  return (
    machine !== null &&
    machine.isDeleted !== true &&
    machine.authenticationEnabled !== false
  );
}

/**
 * The machine a key opens, or null. The key is looked up by its public
 * selector in `machine_api_keys`; a revoked or expired key, or a key of a
 * deleted or disabled machine, opens nothing, and the secret is then checked
 * by the native HMAC verifier (constant time). A selector found in the table
 * is decided by the table alone.
 *
 * A selector the table does not hold may be the single key of a machine
 * written before ANH-165, still in `machines.apiKey` until the migration
 * `migrations/machineApiKeys:moveMachineKeysToTable` has run: it is checked
 * there, under the same machine rules.
 */
export async function authenticateMachine(ctx: QueryCtx, apiKey: string) {
  const selector = apiKeySelector(apiKey);
  if (!selector) return null;
  const now = Date.now();
  const key = await ctx.db
    .query("machine_api_keys")
    .withIndex("by_selector", (q) => q.eq("selector", selector))
    .unique();
  if (key !== null) {
    if (!keyIsValid(key, now)) return null;
    const machine = await ctx.db.get(key.machineId);
    if (!mayAuthenticate(machine)) return null;
    if (!(await verifyApiKey(apiKey, key.keyHash))) return null;
    return {
      _id: machine._id,
      name: machine.name,
      status: machine.status,
      key: {
        id: key._id,
        expiresAt: key.expiresAt,
        lastUsedAt: key.lastUsedAt,
      },
    };
  }
  const machine = await ctx.db
    .query("machines")
    .withIndex("by_apiKeySelector", (q) => q.eq("apiKeySelector", selector))
    .unique();
  if (!mayAuthenticate(machine) || machine.apiKey === undefined) return null;
  if (!(await verifyApiKey(apiKey, machine.apiKey))) return null;
  return {
    _id: machine._id,
    name: machine.name,
    status: machine.status,
  };
}
