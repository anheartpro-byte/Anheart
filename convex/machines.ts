import { limitUser } from "./lib/rateLimits";
import {
  query,
  mutation,
  internalQuery,
  internalMutation,
  type MutationCtx,
} from "./_generated/server";
import { v } from "convex/values";
import { audit, userActor } from "./lib/audit";
import type { Doc, Id } from "./_generated/dataModel";
import { machineHealthValidator, HEARTBEAT_RETENTION_MS } from "./lib/machineHealth";
import { recordMachineHealth } from "./machineHealth";
import {
  ORGANIZATION_ADMIN_ROLES,
  STAFF_ROLES,
  requireRole,
  getCurrentUserOrThrow,
  canAccessMachine,
  canManageMachine,
  inScope,
  organizationRoleOf,
  requireGestionnaireAdmin,
  sameOrganization,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import { insertMachineKey } from "./lib/machineKeys";
import { authenticateMachine, authenticatedMachine } from "./lib/machineAuth";
import { scheduleAlert } from "./lib/notifications";
import { interruptSilentSessions, resumeNamedSession } from "./training";
import {
  currentMedicalParameters,
  medicalParametersStatus,
} from "./lib/medicalParameters";
// One threshold for the server's job and for the dashboard, which reads the
// age of `lastHeartbeat` on `serverNow` (hooks/use-freshness.ts).
import { LIVE_FRESH_MS } from "../lib/training";

/**
 * Make `gestionnaireIds` the exact list of a machine's gestionnaires, writing
 * only what differs from the rows already there.
 *
 * Only the gestionnaires of the machine's organisation are kept, each once, in
 * the order given; the first of them owns the machine. A row that already
 * links a kept gestionnaire stays the same row, with its author and its date
 * (its `isOwner` follows the new first place). A row whose gestionnaire is not
 * kept is deleted, and so is a second row for the same gestionnaire.
 */
async function writeMachineGestionnaires(
  ctx: MutationCtx,
  machine: Pick<Doc<"machines">, "_id" | "organizationId">,
  gestionnaireIds: readonly Id<"users">[],
  createdBy: Id<"users">,
): Promise<{
  added: Id<"users">[];
  removed: Id<"users">[];
  changed: boolean;
  owner: Id<"users"> | undefined;
}> {
  const kept: Id<"users">[] = [];
  for (const gestionnaireId of new Set(gestionnaireIds)) {
    const role = await organizationRoleOf(
      ctx,
      gestionnaireId,
      machine.organizationId,
    );
    if (role === "gestionnaire") kept.push(gestionnaireId);
  }
  const owner = kept[0];

  const existing = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
    .collect();
  const linked = new Set<Id<"users">>();
  const added: Id<"users">[] = [];
  const removed = [
    ...new Set(existing.map((row) => row.gestionnaireId)),
  ].filter((id) => !kept.includes(id));
  let changed = false;
  for (const row of existing) {
    if (!kept.includes(row.gestionnaireId) || linked.has(row.gestionnaireId)) {
      await ctx.db.delete(row._id);
      changed = true;
      continue;
    }
    linked.add(row.gestionnaireId);
    const isOwner = row.gestionnaireId === owner;
    if (row.isOwner !== isOwner) {
      await ctx.db.patch(row._id, { isOwner });
      changed = true;
    }
  }

  const now = Date.now();
  for (const gestionnaireId of kept) {
    if (linked.has(gestionnaireId)) continue;
    await ctx.db.insert("machine_gestionnaires", {
      organizationId: machine.organizationId,
      machineId: machine._id,
      gestionnaireId,
      isOwner: gestionnaireId === owner,
      createdAt: now,
      createdBy,
    });
    added.push(gestionnaireId);
    changed = true;
  }
  return { added, removed, changed, owner };
}

/**
 * Create a new machine (Raspberry Pi) - Anheart admin only
 * Admin creates machines and assigns them to gestionnaires
 *
 * The machine belongs to exactly one organisation: `organizationId`, or the
 * admin's own when it is not given. Only gestionnaires of that organisation
 * can be assigned.
 */
export const createMachine = mutation({
  args: {
    name: v.string(),
    location: v.optional(v.string()),
    gestionnaireIds: v.optional(v.array(v.id("users"))), // Gestionnaires to assign this machine to
    organizationId: v.optional(v.id("organizations")), // Defaults to the admin's organisation
  },
  returns: v.object({
    machineId: v.id("machines"),
    apiKey: v.string(),
  }),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await requireRole(ctx, ["admin"]);

    const organizationId = args.organizationId ?? currentUser.organizationId;
    // An organisation deleted in Clerk takes no new machine: its members are
    // inactive and none of them could use it.
    const organization = await ctx.db.get(organizationId);
    if (!organization || organization.deletedAt !== undefined) {
      throw refusal("organization_not_found");
    }

    const now = Date.now();

    const machineId = await ctx.db.insert("machines", {
      organizationId,
      name: args.name,
      status: "offline",
      lastHeartbeat: 0,
      location: args.location,
      createdAt: now,
    });
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId,
      action: "machine.created",
      targetId: machineId,
    });
    // Its first key, in `machine_api_keys` like every key (ANH-165).
    const { apiKey, keyId } = await insertMachineKey(ctx, machineId, {
      createdBy: currentUser._id,
      expiresAt: undefined,
      now,
    });
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId,
      action: "machine_key.created",
      targetId: keyId,
      details: { machineId, count: 0 },
    });

    // Assign machine to gestionnaires: the first one kept owns it.
    const links = await writeMachineGestionnaires(
      ctx,
      { _id: machineId, organizationId },
      args.gestionnaireIds ?? [],
      currentUser._id,
    );
    if (links.changed) {
      await audit(ctx, {
        actor: userActor(currentUser),
        source: "site",
        organizationId,
        action: "machine_gestionnaires.set",
        targetId: machineId,
        details: { added: links.added },
      });
    }

    // Return plain API key - this is the only time it's visible!
    return {
      machineId,
      apiKey,
    };
  },
});

/**
 * Set the exact list of a machine's gestionnaires (Admin only: Anheart's, or
 * the admin of the machine's organisation). Only gestionnaires of the
 * machine's organisation are assigned, each once even when sent twice; the
 * first of them owns the machine. A gestionnaire who keeps the machine keeps
 * their row as it is: only the difference is written.
 */
export const assignMachineToGestionnaires = mutation({
  args: {
    machineId: v.id("machines"),
    gestionnaireIds: v.array(v.id("users")),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);

    const machine = await ctx.db.get(args.machineId);
    if (!machine || !inScope(currentUser, machine.organizationId)) {
      throw refusal("machine_not_found");
    }

    const links = await writeMachineGestionnaires(
      ctx,
      machine,
      args.gestionnaireIds,
      currentUser._id,
    );
    if (links.changed) {
      await audit(ctx, {
        actor: userActor(currentUser),
        source: "site",
        organizationId: machine.organizationId,
        action: "machine_gestionnaires.set",
        targetId: machine._id,
        details: {
          added: links.added,
          removed: links.removed,
          gestionnaireId: links.owner,
        },
      });
    }
    return null;
  },
});

/**
 * Set the exact list of machines a gestionnaire manages (admin only: the
 * Anheart admin, or the admin of the gestionnaire's organisation), among the
 * machines of that organisation.
 *
 * Only this gestionnaire's rows of `machine_gestionnaires` are read and
 * written: a machine added here keeps its other gestionnaires, and a machine
 * removed here stays managed by them. A link that already exists is left as it
 * is, with its `isOwner`. A new link is never an owner link.
 *
 * A deleted machine is outside the decision: its link is kept whether or not
 * it is named (the dashboard lists no deleted machine, and a restored machine
 * gets its gestionnaires back), and a deleted machine is never linked.
 * A link to a machine gone from the table is removed even when named. Such
 * cleanup is audited, but does not count in the dashboard's removed total.
 */
export const setGestionnaireMachines = mutation({
  args: {
    gestionnaireId: v.id("users"),
    machineIds: v.array(v.id("machines")),
  },
  returns: v.object({
    added: v.number(),
    removed: v.number(),
  }),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const { currentUser, organizationId } = await requireGestionnaireAdmin(
      ctx,
      args.gestionnaireId,
    );

    // Only the links of the organisation the decision applies to: what this
    // gestionnaire manages in another organisation is not read nor written.
    const relations = (
      await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", args.gestionnaireId),
        )
        .collect()
    ).filter((r) => r.organizationId === organizationId);
    // Deleted machines stay outside the decision; missing machines leave
    // orphaned links to clean up, whether or not their IDs are requested.
    const removable: typeof relations = [];
    const orphaned: typeof relations = [];
    for (const relation of relations) {
      const machine = await ctx.db.get(relation.machineId);
      if (!machine) orphaned.push(relation);
      else if (!machine.isDeleted) removable.push(relation);
    }

    const wanted = new Set(args.machineIds);
    const linked = new Set(relations.map((r) => r.machineId));

    const toAdd = [...wanted].filter((machineId) => !linked.has(machineId));
    const visibleToRemove = removable.filter((r) => !wanted.has(r.machineId));
    const toRemove = [...visibleToRemove, ...orphaned];

    // Every machine to link must exist, in that organisation, and not be
    // deleted, before anything is written. A machine of another organisation
    // is a machine that does not exist.
    for (const machineId of toAdd) {
      const machine = await ctx.db.get(machineId);
      if (
        !machine ||
        machine.isDeleted ||
        machine.organizationId !== organizationId
      ) {
        throw refusal("machine_not_found");
      }
    }

    for (const relation of toRemove) {
      await ctx.db.delete(relation._id);
    }

    const now = Date.now();
    for (const machineId of toAdd) {
      await ctx.db.insert("machine_gestionnaires", {
        organizationId,
        machineId,
        gestionnaireId: args.gestionnaireId,
        isOwner: false,
        createdAt: now,
        createdBy: currentUser._id,
      });
    }

    if (toAdd.length > 0 || toRemove.length > 0) {
      await audit(ctx, {
        actor: userActor(currentUser),
        source: "site",
        organizationId,
        action: "gestionnaire_machines.set",
        targetId: args.gestionnaireId,
        details: {
          added: toAdd,
          removed: [...new Set(toRemove.map((r) => r.machineId))],
        },
      });
    }
    return {
      added: toAdd.length,
      removed: new Set(visibleToRemove.map((r) => r.machineId)).size,
    };
  },
});

/**
 * Get machine details (without API key)
 */
export const getMachine = query({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.union(
    v.object({
      _id: v.id("machines"),
      _creationTime: v.number(),
      // The organisation the machine belongs to: only its gestionnaires can
      // be assigned (the machine form lists those).
      organizationId: v.optional(v.id("organizations")),
      name: v.string(),
      status: v.union(
        v.literal("online"),
        v.literal("offline"),
        v.literal("in_session"),
      ),
      lastHeartbeat: v.number(),
      location: v.optional(v.string()),
      createdAt: v.number(),
      isDeleted: v.optional(v.boolean()),
      deletedAt: v.optional(v.number()),
      softwareVersion: v.optional(v.string()),
      contractVersion: v.optional(v.string()),
      lastVersionSeenAt: v.optional(v.number()),
      medicalParametersVersion: v.optional(v.union(v.string(), v.null())),
      medicalParametersStatus: v.union(
        v.literal("unknown"),
        v.literal("unsigned"),
        v.literal("no_reference"),
        v.literal("current"),
        v.literal("obsolete"),
        v.literal("ahead"),
      ),
      medicalParametersReferenceVersion: v.union(v.string(), v.null()),
      // The server's clock in this answer: what `lastHeartbeat` is aged on.
      serverNow: v.number(),
      gestionnaires: v.array(
        v.object({
          _id: v.id("users"),
          firstName: v.string(),
          lastName: v.string(),
          isOwner: v.boolean(),
        }),
      ),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;

    // Check if machine is deleted - only admin can see deleted machines
    if (machine.isDeleted && currentUser.role !== "admin") {
      return null;
    }

    const hasAccess = await canAccessMachine(ctx, args.machineId, currentUser);
    if (!hasAccess) {
      return null;
    }

    const medical = await currentMedicalParameters(ctx);

    // Get all gestionnaires for this machine
    const relations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();

    const gestionnaires = await Promise.all(
      relations.map(async (r) => {
        const g = await ctx.db.get(r.gestionnaireId);
        if (!g) return null;
        return {
          _id: g._id,
          firstName: g.firstName,
          lastName: g.lastName,
          isOwner: r.isOwner,
        };
      }),
    );

    // Return without API key. The owner comes first: the machine form sends
    // the list back in this order, and the server makes its first one the
    // owner (`assignMachineToGestionnaires` keeps rows, so their order alone
    // no longer says who owns the machine).
    const validGestionnaires = gestionnaires
      .filter((g): g is NonNullable<typeof g> => g !== null)
      .sort((a, b) => Number(b.isOwner) - Number(a.isOwner));

    return {
      _id: machine._id,
      _creationTime: machine._creationTime,
      organizationId: machine.organizationId,
      name: machine.name,
      status: machine.status,
      lastHeartbeat: machine.lastHeartbeat,
      location: machine.location,
      createdAt: machine.createdAt,
      isDeleted: machine.isDeleted,
      deletedAt: machine.deletedAt,
      softwareVersion: machine.softwareVersion,
      contractVersion: machine.contractVersion,
      lastVersionSeenAt: machine.lastVersionSeenAt,
      medicalParametersVersion: machine.medicalParametersVersion,
      medicalParametersStatus: medicalParametersStatus(
        machine.medicalParametersVersion,
        medical?.version ?? null,
      ),
      medicalParametersReferenceVersion: medical?.version ?? null,
      serverNow: Date.now(),
      gestionnaires: validGestionnaires,
    };
  },
});

/**
 * List machines with role-based filtering
 */
export const listMachines = query({
  args: {
    status: v.optional(
      v.union(
        v.literal("online"),
        v.literal("offline"),
        v.literal("in_session"),
      ),
    ),
    includeDeleted: v.optional(v.boolean()), // Admin only - include deleted machines
  },
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      lastHeartbeat: v.number(),
      // The server's clock in this answer: what `lastHeartbeat` is aged on.
      serverNow: v.number(),
      location: v.optional(v.string()),
      isDeleted: v.optional(v.boolean()),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    let allMachines;

    if (currentUser.role === "admin") {
      // Anheart admin sees all machines, optionally including deleted ones
      allMachines = await ctx.db.query("machines").collect();
      if (!args.includeDeleted) {
        allMachines = allMachines.filter((m) => !m.isDeleted);
      }
    } else if (currentUser.role === "org_admin") {
      // Organisation admin sees the machines of their organisation (never deleted ones)
      allMachines = (
        await ctx.db
          .query("machines")
          .withIndex("by_organization", (q) =>
            q.eq("organizationId", currentUser.organizationId),
          )
          .collect()
      ).filter((m) => !m.isDeleted);
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees machines they manage via relation table (never deleted ones)
      const relations = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser._id),
        )
        .collect();

      const managed = await Promise.all(
        relations.map((r) => ctx.db.get(r.machineId)),
      );
      allMachines = managed.filter(
        (m): m is NonNullable<typeof m> =>
          m !== null &&
          sameOrganization(currentUser, m.organizationId) &&
          !m.isDeleted,
      );
    } else {
      // Users don't see machines
      return [];
    }

    if (args.status) {
      allMachines = allMachines.filter((m) => m.status === args.status);
    }

    const serverNow = Date.now();
    return allMachines.map((m) => ({
      _id: m._id,
      name: m.name,
      status: m.status,
      lastHeartbeat: m.lastHeartbeat,
      serverNow,
      location: m.location,
      isDeleted: m.isDeleted,
    }));
  },
});

/**
 * Update a machine's name or location
 */
export const updateMachine = mutation({
  args: {
    machineId: v.id("machines"),
    name: v.optional(v.string()),
    location: v.optional(v.string()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await getCurrentUserOrThrow(ctx);
    const canManage = await canManageMachine(ctx, args.machineId, currentUser);
    if (!canManage) {
      throw refusal("machine_not_managed");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw refusal("machine_not_found");
    }

    const updates: Record<string, unknown> = {};
    if (args.name !== undefined) updates.name = args.name;
    if (args.location !== undefined) updates.location = args.location;

    if (Object.keys(updates).length > 0) {
      await ctx.db.patch(args.machineId, updates);
    }

    return null;
  },
});

/**
 * Soft delete a machine (marks as deleted, can be restored by admin)
 */
export const deleteMachine = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await getCurrentUserOrThrow(ctx);
    const canManage = await canManageMachine(ctx, args.machineId, currentUser);
    if (!canManage) {
      throw refusal("machine_not_managed");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw refusal("machine_not_found");
    }

    // Check for active sessions
    if (machine.status === "in_session") {
      throw refusal("machine_in_session");
    }

    // Check for any pending sessions
    const pendingSessions = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .first();

    if (pendingSessions) {
      throw refusal("machine_has_pending_session");
    }

    // Soft delete - mark as deleted instead of actually deleting
    await ctx.db.patch(args.machineId, {
      isDeleted: true,
      deletedAt: Date.now(),
      deletedBy: currentUser._id,
      status: "offline", // Set to offline when deleted
    });
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine.deleted",
      targetId: args.machineId,
    });

    return null;
  },
});

/**
 * Restore a deleted machine (admin only)
 */
export const restoreMachine = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await requireRole(ctx, ["admin"]);

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw refusal("machine_not_found");
    }

    if (!machine.isDeleted) {
      throw refusal("machine_not_deleted");
    }

    // Restore the machine
    await ctx.db.patch(args.machineId, {
      isDeleted: false,
      deletedAt: undefined,
      deletedBy: undefined,
    });
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine.restored",
      targetId: args.machineId,
    });

    return null;
  },
});

/**
 * Get machine by API key (for HTTP endpoints): a key of `machine_api_keys`
 * that is neither revoked nor expired, of a machine neither deleted nor
 * disabled (convex/lib/machineAuth.ts). Reads only; returns the key's raw
 * expiry and last use for the HTTP action to judge on its own clock.
 */
export const getMachineByApiKey = internalQuery({
  args: {
    apiKey: v.string(),
  },
  returns: authenticatedMachine,
  handler: (ctx, args) => authenticateMachine(ctx, args.apiKey),
});

/**
 * Validate machine API key from HTTP request
 * Takes the complete credential and returns a machine only after verification.
 */
export const validateMachineApiKey = internalQuery({
  args: {
    apiKey: v.string(),
  },
  returns: authenticatedMachine,
  handler: (ctx, args) => authenticateMachine(ctx, args.apiKey),
});

/**
 * Record a heartbeat from a machine
 */
export const recordHeartbeat = internalMutation({
  args: {
    machineId: v.id("machines"),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.string()),
    softwareVersion: v.optional(v.string()),
    contractVersion: v.optional(v.string()),
    medicalParametersVersion: v.optional(v.union(v.string(), v.null())),
    health: v.optional(machineHealthValidator),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const now = Date.now();
    const machine = await ctx.db.get(args.machineId);

    if (!machine) {
      // Read by the heartbeat route, which answers the machine with its own
      // codes (`refusalOf`, convex/lib/contract.ts): not a dashboard refusal.
      throw new Error("Machine not found");
    }

    // Determine new status
    let newStatus: "online" | "in_session" = "online";
    if (args.activeSessionId) {
      newStatus = "in_session";
    }

    // Update machine. A heartbeat that went through the contract gate states
    // the versions as they are now: a software version it did not send is
    // cleared rather than left to look current.
    await ctx.db.patch(args.machineId, {
      lastHeartbeat: now,
      status: newStatus,
      health: args.health,
      healthUpdatedAt: args.health === undefined ? undefined : now,
      ...(args.contractVersion === undefined
        ? {}
        : {
            softwareVersion: args.softwareVersion,
            contractVersion: args.contractVersion,
            medicalParametersVersion: args.medicalParametersVersion,
            lastVersionSeenAt: now,
          }),
    });

    // Record heartbeat history
    const sessionId = args.activeSessionId
      ? (ctx.db.normalizeId("sessions", args.activeSessionId) ?? undefined)
      : undefined;

    await ctx.db.insert("machine_heartbeats", {
      machineId: args.machineId,
      timestamp: now,
      batteryLevel: args.batteryLevel,
      wifiStrength: args.wifiStrength,
      activeSessionId: sessionId,
    });
    // The machine runs this session: if the server had marked it interrupted
    // for lack of news, it is active again.
    if (sessionId !== undefined) {
      await resumeNamedSession(ctx, args.machineId, sessionId);
    }

    if (args.health !== undefined) await recordMachineHealth(ctx, machine, args.health, now);
    const expired = await ctx.db.query("machine_heartbeats")
      .withIndex("by_machine_and_timestamp", (q) =>
        q.eq("machineId", args.machineId).lte("timestamp", now - HEARTBEAT_RETENTION_MS))
      .take(100);
    for (const heartbeat of expired) await ctx.db.delete(heartbeat._id);

    return null;
  },
});

/**
 * Check for machines that have gone offline, then for training sessions the
 * server has had no news of for `SESSION_SILENCE_MS` (marked interrupted, end
 * unknown: `training.interruptSilentSessions`).
 * Called by cron job every minute. A machine marked offline is an alert to
 * its gestionnaires and its organisation's admins (`machine_offline`, ANH-90).
 */
export const checkOfflineMachines = internalMutation({
  args: {},
  returns: v.object({
    checkedCount: v.number(),
    markedOfflineCount: v.number(),
    interruptedSessionCount: v.number(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const cutoff = now - LIVE_FRESH_MS;

    // Get online machines
    const onlineMachines = await ctx.db
      .query("machines")
      .withIndex("by_status", (q) => q.eq("status", "online"))
      .collect();

    // Get in_session machines
    const inSessionMachines = await ctx.db
      .query("machines")
      .withIndex("by_status", (q) => q.eq("status", "in_session"))
      .collect();

    const allActiveMachines = [...onlineMachines, ...inSessionMachines];
    let markedOfflineCount = 0;

    for (const machine of allActiveMachines) {
      if (machine.lastHeartbeat < cutoff) {
        await ctx.db.patch(machine._id, { status: "offline" });
        markedOfflineCount++;
        await scheduleAlert(ctx, machine._id, "machine_offline");

        console.log(
          `Machine ${machine.name} marked offline (last heartbeat: ${new Date(machine.lastHeartbeat).toISOString()})`,
        );
      }
    }

    return {
      checkedCount: allActiveMachines.length,
      markedOfflineCount,
      interruptedSessionCount: await interruptSilentSessions(ctx, now),
    };
  },
});

/**
 * Get recent heartbeats for a machine (for debugging/monitoring)
 */
export const getRecentHeartbeats = query({
  args: {
    machineId: v.id("machines"),
    limit: v.optional(v.number()),
  },
  returns: v.array(
    v.object({
      _id: v.id("machine_heartbeats"),
      timestamp: v.number(),
      batteryLevel: v.optional(v.number()),
      wifiStrength: v.optional(v.number()),
      activeSessionId: v.optional(v.id("sessions")),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const hasAccess = await canAccessMachine(ctx, args.machineId, currentUser);
    if (!hasAccess) {
      return [];
    }

    const limit = args.limit ?? 10;

    const heartbeats = await ctx.db
      .query("machine_heartbeats")
      .withIndex("by_machine_and_timestamp", (q) =>
        q.eq("machineId", args.machineId),
      )
      .order("desc")
      .take(limit);

    return heartbeats.map((h) => ({
      _id: h._id,
      timestamp: h.timestamp,
      batteryLevel: h.batteryLevel,
      wifiStrength: h.wifiStrength,
      activeSessionId: h.activeSessionId,
    }));
  },
});

/**
 * Assign a gestionnaire to a machine (admin or owner gestionnaire)
 */
export const assignGestionnaireToMachine = mutation({
  args: {
    machineId: v.id("machines"),
    gestionnaireId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check permissions - the machine is one the caller manages
    if (!(await canManageMachine(ctx, args.machineId, currentUser))) {
      throw refusal("machine_not_managed");
    }

    // Verify machine exists
    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw refusal("machine_not_found");
    }

    // Verify gestionnaire has the correct role in the machine's organisation
    const role = await organizationRoleOf(
      ctx,
      args.gestionnaireId,
      machine.organizationId,
    );
    if (role !== "gestionnaire") {
      throw refusal("not_a_gestionnaire");
    }

    // Check if relation already exists
    const existingRelation = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine_and_gestionnaire", (q) =>
        q
          .eq("machineId", args.machineId)
          .eq("gestionnaireId", args.gestionnaireId),
      )
      .unique();

    if (existingRelation) {
      throw refusal("gestionnaire_already_assigned");
    }

    // Create relation
    await ctx.db.insert("machine_gestionnaires", {
      organizationId: machine.organizationId,
      machineId: args.machineId,
      gestionnaireId: args.gestionnaireId,
      isOwner: false, // New assignments are not owners
      createdAt: Date.now(),
      createdBy: currentUser._id,
    });
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId: machine.organizationId,
      action: "machine_gestionnaire.assigned",
      targetId: args.machineId,
      details: { gestionnaireId: args.gestionnaireId },
    });

    return null;
  },
});

/**
 * Remove a gestionnaire from a machine
 */
export const removeGestionnaireFromMachine = mutation({
  args: {
    machineId: v.id("machines"),
    gestionnaireId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check permissions
    if (!(await canManageMachine(ctx, args.machineId, currentUser))) {
      throw refusal("machine_not_managed");
    }
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire cannot remove themselves if they are the only one
      if (args.gestionnaireId === currentUser._id) {
        const allRelations = await ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
          .collect();
        if (allRelations.length <= 1) {
          throw refusal("last_gestionnaire");
        }
      }
    }

    // Find and delete the relation
    const relation = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine_and_gestionnaire", (q) =>
        q
          .eq("machineId", args.machineId)
          .eq("gestionnaireId", args.gestionnaireId),
      )
      .unique();

    if (!relation) {
      throw refusal("gestionnaire_not_assigned");
    }

    await ctx.db.delete(relation._id);
    await audit(ctx, {
      actor: userActor(currentUser),
      source: "site",
      organizationId: relation.organizationId,
      action: "machine_gestionnaire.removed",
      targetId: args.machineId,
      details: { gestionnaireId: args.gestionnaireId },
    });

    return null;
  },
});

/**
 * Get all gestionnaires for a machine
 */
export const getGestionnairesForMachine = query({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.array(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      isOwner: v.boolean(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const hasAccess = await canAccessMachine(ctx, args.machineId, currentUser);
    if (!hasAccess) {
      return [];
    }

    const relations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();

    const gestionnaires = await Promise.all(
      relations.map(async (r) => {
        const g = await ctx.db.get(r.gestionnaireId);
        if (!g) return null;
        return {
          _id: g._id,
          firstName: g.firstName,
          lastName: g.lastName,
          email: g.email,
          isOwner: r.isOwner,
        };
      }),
    );

    return gestionnaires.filter((g): g is NonNullable<typeof g> => g !== null);
  },
});

/**
 * Get all machines for a gestionnaire, deleted machines left out
 */
export const getMachinesForGestionnaire = query({
  args: {
    gestionnaireId: v.optional(v.id("users")), // If not provided, uses current user
  },
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      lastHeartbeat: v.number(),
      // The server's clock in this answer: what `lastHeartbeat` is aged on.
      serverNow: v.number(),
      location: v.optional(v.string()),
      isOwner: v.boolean(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Determine which gestionnaire to query for
    let targetGestionnaireId = args.gestionnaireId;

    // Admin can query any gestionnaire, gestionnaires only their own
    if (currentUser.role === "gestionnaire") {
      targetGestionnaireId ??= currentUser._id;
      if (targetGestionnaireId !== currentUser._id) {
        return [];
      }
    } else if (!ORGANIZATION_ADMIN_ROLES.includes(currentUser.role)) {
      return [];
    }

    if (!targetGestionnaireId) {
      // Admin didn't specify a gestionnaire - return empty
      return [];
    }

    const relations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_gestionnaire", (q) =>
        q.eq("gestionnaireId", targetGestionnaireId!),
      )
      .collect();

    const serverNow = Date.now();
    const machines = await Promise.all(
      relations.map(async (r) => {
        const m = await ctx.db.get(r.machineId);
        // Only the machines of an organisation the caller may see, and never
        // a deleted one: its link is kept for a restore, not shown.
        if (!m || m.isDeleted || !inScope(currentUser, m.organizationId)) {
          return null;
        }
        return {
          _id: m._id,
          name: m.name,
          status: m.status,
          lastHeartbeat: m.lastHeartbeat,
          serverNow,
          location: m.location,
          isOwner: r.isOwner,
        };
      }),
    );

    return machines.filter((m): m is NonNullable<typeof m> => m !== null);
  },
});
