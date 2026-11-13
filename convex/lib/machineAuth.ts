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
    // Authenticating never writes.
    key: v.object({
      id: v.id("machine_api_keys"),
      expiresAt: v.optional(v.number()),
      lastUsedAt: v.optional(v.number()),
    }),
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
 * selector in `machine_api_keys`, the only place a machine key lives
 * (ANH-238): a selector the table does not hold opens nothing. A revoked or
 * expired key, or a key of a deleted or disabled machine, opens nothing, and
 * the secret is then checked by the native HMAC verifier (constant time).
 */
export async function authenticateMachine(ctx: QueryCtx, apiKey: string) {
  const selector = apiKeySelector(apiKey);
  if (!selector) return null;
  const key = await ctx.db
    .query("machine_api_keys")
    .withIndex("by_selector", (q) => q.eq("selector", selector))
    .unique();
  if (key === null || !keyIsValid(key, Date.now())) return null;
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
