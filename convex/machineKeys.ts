/**
 * The keys of the machines (ANH-165): list them, create one (shown once,
 * with an overlap during which the keys it replaces stay valid), revoke one at
 * once. Rules of the keys themselves: `convex/lib/machineKeys.ts`; how a
 * request is authenticated: `convex/lib/machineAuth.ts`.
 *
 * On the dashboard, the Anheart admin manages the keys of every machine, the
 * admin of an organisation those of the machines of their organisation, and
 * no one else. The plain key is in the answer of the call that created it,
 * and nowhere else: no list, no log, no error carries it.
 */
import { v } from "convex/values";
import {
  internalMutation,
  mutation,
  query,
  type MutationCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import {
  getCurrentUserOrThrow,
  inScope,
  ORGANIZATION_ADMIN_ROLES,
  requireRole,
  type CurrentUser,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import { limitUser } from "./lib/rateLimits";
import { audit, SYSTEM_ACTOR, userActor } from "./lib/audit";
import {
  EXPIRED_KEYS_PER_RUN,
  keyState,
  keyUseIsDue,
  LISTED_KEYS,
  rotateMachineKey,
  rotationOptions,
} from "./lib/machineKeys";

const keyStateValidator = v.union(
  v.literal("active"),
  v.literal("replaced"),
  v.literal("expired"),
  v.literal("revoked"),
);

const issuedKey = v.object({
  keyId: v.id("machine_api_keys"),
  // The plain key: in this answer only.
  apiKey: v.string(),
  prefix: v.string(),
});

const rotationArgs = {
  machineId: v.id("machines"),
  // How long the keys the new one replaces stay valid, in minutes: 24 h when
  // absent, 0 to revoke them at once, 7 days at most.
  overlapMinutes: v.optional(v.number()),
  // How long the new key lives, in minutes counted on the server's clock:
  // one hour to ten years; absent for a key that does not expire.
  lifetimeMinutes: v.optional(v.number()),
};

/**
 * The machine whose keys the caller manages, or the refusal
 * `machine_not_found`: a machine that does not exist, is deleted, or belongs
 * to an organisation the caller does not administer answers the same.
 */
async function managedMachine(
  ctx: MutationCtx,
  caller: CurrentUser,
  machineId: Id<"machines">,
): Promise<Doc<"machines">> {
  const machine = await ctx.db.get(machineId);
  if (
    !machine ||
    machine.isDeleted ||
    !inScope(caller, machine.organizationId)
  ) {
    throw refusal("machine_not_found");
  }
  return machine;
}

/**
 * The keys of a machine, most recent first (50 at most): prefix, state at the
 * server's clock, creation and its author, last use, expiry, revocation. Never
 * the key, its selector or its verifier. `[]` for anyone but an admin of the
 * machine's organisation (or the Anheart admin), and for a machine that does
 * not exist; a deleted machine's keys are listed to the Anheart admin only.
 *
 * The state is computed when the query runs: an overlap that ends while the
 * page is open shows "expired" once the job has revoked the key (within
 * 5 minutes), or at the next reading.
 */
export const listMachineKeys = query({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      _id: v.id("machine_api_keys"),
      prefix: v.string(),
      state: keyStateValidator,
      createdAt: v.number(),
      createdByName: v.union(v.string(), v.null()),
      lastUsedAt: v.union(v.number(), v.null()),
      expiresAt: v.union(v.number(), v.null()),
      revokedAt: v.union(v.number(), v.null()),
    }),
  ),
  handler: async (ctx, args) => {
    const caller = await getCurrentUserOrThrow(ctx);
    if (!ORGANIZATION_ADMIN_ROLES.includes(caller.role)) return [];
    const machine = await ctx.db.get(args.machineId);
    if (
      !machine ||
      !inScope(caller, machine.organizationId) ||
      (machine.isDeleted && caller.role !== "admin")
    ) {
      return [];
    }
    const now = Date.now();
    const keys = await ctx.db
      .query("machine_api_keys")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .order("desc")
      .take(LISTED_KEYS);
    return await Promise.all(
      keys.map(async (key) => {
        const author =
          key.createdBy === undefined ? null : await ctx.db.get(key.createdBy);
        return {
          _id: key._id,
          prefix: key.prefix,
          state: keyState(key, now),
          createdAt: key.createdAt,
          createdByName:
            author === null ? null : `${author.firstName} ${author.lastName}`,
          lastUsedAt: key.lastUsedAt ?? null,
          expiresAt: key.expiresAt ?? null,
          revokedAt: key.revokedAt ?? null,
        };
      }),
    );
  },
});

/**
 * Create a key for the machine and answer it, the only time it is shown.
 * Every key the machine holds that is still valid stays valid for the
 * overlap (`overlapMinutes`, 24 h by default) and is then revoked by the job
 * `revokeExpiredKeys`; with an overlap of 0 they are revoked at once.
 * Admins only: the Anheart admin, or the admin of the machine's organisation.
 */
export const createMachineKey = mutation({
  args: rotationArgs,
  returns: issuedKey,
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await managedMachine(ctx, caller, args.machineId);
    const now = Date.now();
    const options = rotationOptions(args, now);
    const { issued, replaced } = await rotateMachineKey(ctx, machine, {
      ...options,
      createdBy: caller._id,
      now,
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_key.created",
      targetId: issued.keyId,
      details: {
        machineId: machine._id,
        count: replaced.length,
        expiresAt: options.expiresAt,
      },
    });
    if (options.overlapMs === 0) {
      for (const keyId of replaced) {
        await audit(ctx, {
          actor: userActor(caller),
          source: "site",
          organizationId: machine.organizationId,
          action: "machine_key.revoked",
          targetId: keyId,
          details: { machineId: machine._id },
        });
      }
    }
    return issued;
  },
});

/**
 * Revoke a key at once: the next request it carries is refused (401). A key
 * already revoked is left as it is. Admins only, as `createMachineKey`; a key
 * of a machine the caller does not administer answers `machine_key_not_found`,
 * as a key that does not exist. A deleted machine's key can be revoked.
 */
export const revokeMachineKey = mutation({
  args: { keyId: v.id("machine_api_keys") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const key = await ctx.db.get(args.keyId);
    const machine = key === null ? null : await ctx.db.get(key.machineId);
    if (
      key === null ||
      machine === null ||
      !inScope(caller, machine.organizationId)
    ) {
      throw refusal("machine_key_not_found");
    }
    if (key.revokedAt === undefined) {
      await ctx.db.patch(key._id, {
        revokedAt: Date.now(),
        revokedBy: caller._id,
      });
      await audit(ctx, {
        actor: userActor(caller),
        source: "site",
        organizationId: machine.organizationId,
        action: "machine_key.revoked",
        targetId: key._id,
        details: { machineId: key.machineId },
      });
    }
    return null;
  },
});

/**
 * Write the use of a key, unless one was written less than a minute ago.
 * Called by the machine routes after authentication (convex/lib/machineHttpAuth.ts),
 * only when the action's clock finds the last use a minute old or more.
 * The caller logs and ignores a failure: accounting must never block a request.
 */
export const recordKeyUse = internalMutation({
  args: { keyId: v.id("machine_api_keys") },
  returns: v.boolean(),
  handler: async (ctx, args) => {
    const key = await ctx.db.get(args.keyId);
    const now = Date.now();
    if (key === null || !keyUseIsDue(key, now)) return false;
    await ctx.db.patch(key._id, { lastUsedAt: now });
    return true;
  },
});

/**
 * Revoke the keys whose expiry has passed: the keys replaced by a rotation,
 * once their overlap is over, and the keys created with an expiry. A key
 * already opens nothing from its expiry on (`convex/lib/machineAuth.ts`):
 * this job writes it down. Run every 5 minutes (`convex/crons.ts`), 100 keys
 * at most per run.
 */
export const revokeExpiredKeys = internalMutation({
  args: {},
  returns: v.object({ revoked: v.number() }),
  handler: async (ctx) => {
    const now = Date.now();
    const expired = await ctx.db
      .query("machine_api_keys")
      .withIndex("by_revoked_and_expiry", (q) =>
        q.eq("revokedAt", undefined).gte("expiresAt", 0).lte("expiresAt", now),
      )
      .take(EXPIRED_KEYS_PER_RUN);
    for (const key of expired) {
      await ctx.db.patch(key._id, { revokedAt: now });
      const machine = await ctx.db.get(key.machineId);
      await audit(ctx, {
        actor: SYSTEM_ACTOR,
        source: "cron",
        organizationId: machine?.organizationId,
        action: "machine_key.expired_revoked",
        targetId: key._id,
        details: { machineId: key.machineId },
      });
    }
    return { revoked: expired.length };
  },
});

/**
 * `createMachineKey` from the command line, with the deployment's key, for
 * whoever operates the deployment (provisioning, a test of the rotation
 * against the development deployment, a dashboard out of reach):
 *
 *     npx convex run machineKeys:createKeyByOperator '{"machineId": "…"}'
 *
 * The plain key is printed once, by that command; the key has no author.
 */
export const createKeyByOperator = internalMutation({
  args: rotationArgs,
  returns: issuedKey,
  handler: async (ctx, args) => {
    const machine = await ctx.db.get(args.machineId);
    if (!machine || machine.isDeleted) throw refusal("machine_not_found");
    const now = Date.now();
    const options = rotationOptions(args, now);
    const { issued, replaced } = await rotateMachineKey(ctx, machine, {
      ...options,
      createdBy: undefined,
      now,
    });
    await audit(ctx, {
      actor: SYSTEM_ACTOR,
      source: "operator",
      organizationId: machine.organizationId,
      action: "machine_key.created",
      targetId: issued.keyId,
      details: {
        machineId: machine._id,
        count: replaced.length,
        expiresAt: options.expiresAt,
      },
    });
    if (options.overlapMs === 0) {
      for (const keyId of replaced) {
        await audit(ctx, {
          actor: SYSTEM_ACTOR,
          source: "operator",
          organizationId: machine.organizationId,
          action: "machine_key.revoked",
          targetId: keyId,
          details: { machineId: machine._id },
        });
      }
    }
    return issued;
  },
});

/** `revokeMachineKey` from the command line, as `createKeyByOperator`. */
export const revokeKeyByOperator = internalMutation({
  args: { keyId: v.id("machine_api_keys") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const key = await ctx.db.get(args.keyId);
    if (key === null) throw refusal("machine_key_not_found");
    if (key.revokedAt === undefined) {
      await ctx.db.patch(key._id, { revokedAt: Date.now() });
      const machine = await ctx.db.get(key.machineId);
      await audit(ctx, {
        actor: SYSTEM_ACTOR,
        source: "operator",
        organizationId: machine?.organizationId,
        action: "machine_key.revoked",
        targetId: key._id,
        details: { machineId: key.machineId },
      });
    }
    return null;
  },
});
