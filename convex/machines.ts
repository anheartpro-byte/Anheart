import {
  query,
  mutation,
  internalQuery,
  internalMutation,
} from "./_generated/server";
import { v } from "convex/values";
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
import { generateApiKey } from "./lib/crypto";
import { authenticateMachine, authenticatedMachine } from "./lib/machineAuth";

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
    const currentUser = await requireRole(ctx, ["admin"]);

    const organizationId = args.organizationId ?? currentUser.organizationId;
    if (!(await ctx.db.get(organizationId))) {
      throw new Error("Organization not found");
    }

    const { plain, hashed, selector } = await generateApiKey();
    const now = Date.now();

    const machineId = await ctx.db.insert("machines", {
      organizationId,
      name: args.name,
      apiKey: hashed,
      apiKeySelector: selector,
      status: "offline",
      lastHeartbeat: 0,
      location: args.location,
      createdAt: now,
    });

    // Assign machine to gestionnaires
    if (args.gestionnaireIds && args.gestionnaireIds.length > 0) {
      for (let i = 0; i < args.gestionnaireIds.length; i++) {
        const gestionnaireId = args.gestionnaireIds[i];
        const role = await organizationRoleOf(
          ctx,
          gestionnaireId,
          organizationId,
        );
        if (role === "gestionnaire") {
          await ctx.db.insert("machine_gestionnaires", {
            organizationId,
            machineId,
            gestionnaireId,
            isOwner: i === 0, // First one is the primary owner
            createdAt: now,
            createdBy: currentUser._id,
          });
        }
      }
    }

    // Return plain API key - this is the only time it's visible!
    return {
      machineId,
      apiKey: plain,
    };
  },
});

/**
 * Assign a machine to gestionnaires (Admin only: Anheart's, or the admin of
 * the machine's organisation). Only gestionnaires of the machine's
 * organisation are assigned.
 */
export const assignMachineToGestionnaires = mutation({
  args: {
    machineId: v.id("machines"),
    gestionnaireIds: v.array(v.id("users")),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);

    const machine = await ctx.db.get(args.machineId);
    if (!machine || !inScope(currentUser, machine.organizationId)) {
      throw new Error("Machine not found");
    }

    const now = Date.now();

    // Remove existing relations
    const existingRelations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();

    for (const relation of existingRelations) {
      await ctx.db.delete(relation._id);
    }

    // Add new relations
    for (let i = 0; i < args.gestionnaireIds.length; i++) {
      const gestionnaireId = args.gestionnaireIds[i];
      const role = await organizationRoleOf(
        ctx,
        gestionnaireId,
        machine.organizationId,
      );
      if (role === "gestionnaire") {
        await ctx.db.insert("machine_gestionnaires", {
          organizationId: machine.organizationId,
          machineId: args.machineId,
          gestionnaireId,
          isOwner: i === 0,
          createdAt: now,
          createdBy: currentUser._id,
        });
      }
    }

    return null;
  },
});

/**
 * Set the exact list of machines a gestionnaire manages (admin only).
 *
 * Only this gestionnaire's rows of `machine_gestionnaires` are read and
 * written: a machine added here keeps its other gestionnaires, and a machine
 * removed here stays managed by them. A link that already exists is left as it
 * is, with its `isOwner`. A new link is never an owner link.
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
    const { currentUser } = await requireGestionnaireAdmin(
      ctx,
      args.gestionnaireId,
    );

    const existingRelations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_gestionnaire", (q) =>
        q.eq("gestionnaireId", args.gestionnaireId),
      )
      .collect();

    const wanted = new Set(args.machineIds);
    const linked = new Set(existingRelations.map((r) => r.machineId));

    const toAdd = [...wanted].filter((machineId) => !linked.has(machineId));
    const toRemove = existingRelations.filter((r) => !wanted.has(r.machineId));

    // Every machine to link must exist before anything is written.
    for (const machineId of toAdd) {
      const machine = await ctx.db.get(machineId);
      if (!machine) {
        throw new Error("Machine not found");
      }
    }

    for (const relation of toRemove) {
      await ctx.db.delete(relation._id);
    }

    const now = Date.now();
    for (const machineId of toAdd) {
      await ctx.db.insert("machine_gestionnaires", {
        machineId,
        gestionnaireId: args.gestionnaireId,
        isOwner: false,
        createdAt: now,
        createdBy: currentUser._id,
      });
    }

    return {
      added: toAdd.length,
      removed: new Set(toRemove.map((r) => r.machineId)).size,
    };
  },
});

/**
 * Regenerate API key for a machine
 * Old API key is immediately invalidated
 */
export const regenerateApiKey = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.object({
    apiKey: v.string(),
  }),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const canManage = await canManageMachine(ctx, args.machineId, currentUser);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Generate new API key
    const { plain, hashed, selector } = await generateApiKey();

    // Update machine with new hashed key
    await ctx.db.patch(args.machineId, {
      apiKey: hashed,
      apiKeySelector: selector,
    });

    // Return plain key (shown only this once!)
    return {
      apiKey: plain,
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

    // Return without API key
    const validGestionnaires = gestionnaires.filter(
      (g): g is NonNullable<typeof g> => g !== null,
    );

    return {
      _id: machine._id,
      _creationTime: machine._creationTime,
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

    return allMachines.map((m) => ({
      _id: m._id,
      name: m.name,
      status: m.status,
      lastHeartbeat: m.lastHeartbeat,
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
    const currentUser = await getCurrentUserOrThrow(ctx);
    const canManage = await canManageMachine(ctx, args.machineId, currentUser);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
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
    const currentUser = await getCurrentUserOrThrow(ctx);
    const canManage = await canManageMachine(ctx, args.machineId, currentUser);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Check for active sessions
    if (machine.status === "in_session") {
      throw new Error("Cannot delete machine with active session");
    }

    // Check for any pending sessions
    const pendingSessions = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .first();

    if (pendingSessions) {
      throw new Error("Cannot delete machine with pending sessions");
    }

    // Soft delete - mark as deleted instead of actually deleting
    await ctx.db.patch(args.machineId, {
      isDeleted: true,
      deletedAt: Date.now(),
      deletedBy: currentUser._id,
      status: "offline", // Set to offline when deleted
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
    await requireRole(ctx, ["admin"]);

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    if (!machine.isDeleted) {
      throw new Error("Machine is not deleted");
    }

    // Restore the machine
    await ctx.db.patch(args.machineId, {
      isDeleted: false,
      deletedAt: undefined,
      deletedBy: undefined,
    });

    return null;
  },
});

/**
 * Get machine by API key (for HTTP endpoints)
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
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const now = Date.now();
    const machine = await ctx.db.get(args.machineId);

    if (!machine) {
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
      ...(args.contractVersion === undefined
        ? {}
        : {
            softwareVersion: args.softwareVersion,
            contractVersion: args.contractVersion,
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

    return null;
  },
});

/**
 * Check for machines that have gone offline
 * Called by cron job every minute
 */
export const checkOfflineMachines = internalMutation({
  args: {},
  returns: v.object({
    checkedCount: v.number(),
    markedOfflineCount: v.number(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const cutoff = now - 90000; // 90 seconds ago

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

        console.log(
          `Machine ${machine.name} marked offline (last heartbeat: ${new Date(machine.lastHeartbeat).toISOString()})`,
        );
      }
    }

    return {
      checkedCount: allActiveMachines.length,
      markedOfflineCount,
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
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check permissions - the machine is one the caller manages
    if (!(await canManageMachine(ctx, args.machineId, currentUser))) {
      throw new Error("Not authorized to manage this machine");
    }

    // Verify machine exists
    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Verify gestionnaire has the correct role in the machine's organisation
    const role = await organizationRoleOf(
      ctx,
      args.gestionnaireId,
      machine.organizationId,
    );
    if (role !== "gestionnaire") {
      throw new Error("Target user is not a gestionnaire");
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
      throw new Error("Gestionnaire already assigned to this machine");
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
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check permissions
    if (!(await canManageMachine(ctx, args.machineId, currentUser))) {
      throw new Error("Not authorized to manage this machine");
    }
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire cannot remove themselves if they are the only one
      if (args.gestionnaireId === currentUser._id) {
        const allRelations = await ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
          .collect();
        if (allRelations.length <= 1) {
          throw new Error(
            "Cannot remove yourself as the only gestionnaire. Delete the machine instead.",
          );
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
      throw new Error("Gestionnaire is not assigned to this machine");
    }

    await ctx.db.delete(relation._id);

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
 * Get all machines for a gestionnaire
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

    const machines = await Promise.all(
      relations.map(async (r) => {
        const m = await ctx.db.get(r.machineId);
        // Only the machines of an organisation the caller may see
        if (!m || !inScope(currentUser, m.organizationId)) return null;
        return {
          _id: m._id,
          name: m.name,
          status: m.status,
          lastHeartbeat: m.lastHeartbeat,
          location: m.location,
          isOwner: r.isOwner,
        };
      }),
    );

    return machines.filter((m): m is NonNullable<typeof m> => m !== null);
  },
});
