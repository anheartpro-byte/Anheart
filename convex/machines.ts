import {
  query,
  mutation,
  internalQuery,
  internalMutation,
} from "./_generated/server";
import { v } from "convex/values";
import {
  requireRole,
  getCurrentUserOrThrow,
  canAccessMachine,
  canManageMachine,
  isGestionnaireOfMachine,
} from "./lib/auth";
import { generateApiKey, hashApiKey } from "./lib/crypto";

/**
 * Create a new machine (Raspberry Pi)
 */
export const createMachine = mutation({
  args: {
    name: v.string(),
    location: v.optional(v.string()),
    config: v.optional(
      v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    ),
    additionalGestionnaireIds: v.optional(v.array(v.id("users"))), // Admin can assign multiple gestionnaires
  },
  returns: v.object({
    machineId: v.id("machines"),
    apiKey: v.string(),
  }),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    const { plain, hashed } = generateApiKey();
    const now = Date.now();

    const machineId = await ctx.db.insert("machines", {
      name: args.name,
      apiKey: hashed,
      status: "offline",
      lastHeartbeat: 0,
      location: args.location,
      config: args.config ?? {
        sampleRate: 1000,
        channels: ["ECG"],
        batchInterval: 1000,
      },
      createdAt: now,
    });

    // Create machine-gestionnaire relation for the creator (as owner)
    if (currentUser.role === "gestionnaire") {
      await ctx.db.insert("machine_gestionnaires", {
        machineId,
        gestionnaireId: currentUser._id,
        isOwner: true,
        createdAt: now,
        createdBy: currentUser._id,
      });
    }

    // Admin can assign additional gestionnaires
    if (currentUser.role === "admin" && args.additionalGestionnaireIds) {
      for (const gestionnaireId of args.additionalGestionnaireIds) {
        const gestionnaire = await ctx.db.get(gestionnaireId);
        if (gestionnaire && gestionnaire.role === "gestionnaire") {
          await ctx.db.insert("machine_gestionnaires", {
            machineId,
            gestionnaireId,
            isOwner:
              args.additionalGestionnaireIds.indexOf(gestionnaireId) === 0, // First one is owner
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
    const canManage = await canManageMachine(ctx, args.machineId);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Generate new API key
    const { plain, hashed } = generateApiKey();

    // Update machine with new hashed key
    await ctx.db.patch(args.machineId, {
      apiKey: hashed,
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
      config: v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
      createdAt: v.number(),
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
    const hasAccess = await canAccessMachine(ctx, args.machineId);
    if (!hasAccess) {
      return null;
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;

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
      config: machine.config,
      createdAt: machine.createdAt,
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
  },
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      lastHeartbeat: v.number(),
      location: v.optional(v.string()),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    let allMachines = await ctx.db.query("machines").collect();

    if (currentUser.role === "admin") {
      // Admin sees all machines
      if (args.status) {
        allMachines = allMachines.filter((m) => m.status === args.status);
      }
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees machines they manage via relation table
      const relations = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser._id),
        )
        .collect();

      const machineIdSet = new Set(
        relations.map((r) => r.machineId.toString()),
      );
      allMachines = allMachines.filter((m) =>
        machineIdSet.has(m._id.toString()),
      );

      if (args.status) {
        allMachines = allMachines.filter((m) => m.status === args.status);
      }
    } else if (
      currentUser.role === "technician" &&
      currentUser.gestionnaireId
    ) {
      // Technician sees their gestionnaire's machines via relation table
      const relations = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser.gestionnaireId!),
        )
        .collect();

      const machineIdSet = new Set(
        relations.map((r) => r.machineId.toString()),
      );
      allMachines = allMachines.filter((m) =>
        machineIdSet.has(m._id.toString()),
      );

      if (args.status) {
        allMachines = allMachines.filter((m) => m.status === args.status);
      }
    } else {
      // Users don't see machines
      return [];
    }

    return allMachines.map((m) => ({
      _id: m._id,
      name: m.name,
      status: m.status,
      lastHeartbeat: m.lastHeartbeat,
      location: m.location,
    }));
  },
});

/**
 * Update machine configuration
 */
export const updateMachine = mutation({
  args: {
    machineId: v.id("machines"),
    name: v.optional(v.string()),
    location: v.optional(v.string()),
    config: v.optional(
      v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    ),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const canManage = await canManageMachine(ctx, args.machineId);
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
    if (args.config !== undefined) updates.config = args.config;

    if (Object.keys(updates).length > 0) {
      await ctx.db.patch(args.machineId, updates);
    }

    return null;
  },
});

/**
 * Delete a machine
 */
export const deleteMachine = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const canManage = await canManageMachine(ctx, args.machineId);
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

    // Delete all machine-gestionnaire relations
    const relations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    for (const relation of relations) {
      await ctx.db.delete(relation._id);
    }

    await ctx.db.delete(args.machineId);
    return null;
  },
});

/**
 * Get machine by API key hash (for HTTP endpoint auth)
 */
export const getMachineByApiKey = internalQuery({
  args: {
    apiKeyHash: v.string(),
  },
  returns: v.union(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      config: v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const machine = await ctx.db
      .query("machines")
      .withIndex("by_api_key", (q) => q.eq("apiKey", args.apiKeyHash))
      .unique();

    if (!machine) return null;

    return {
      _id: machine._id,
      name: machine.name,
      status: machine.status,
      config: machine.config,
    };
  },
});

/**
 * Validate machine API key from HTTP request
 * Takes plain API key, hashes it internally, and returns machine if valid
 */
export const validateMachineApiKey = internalQuery({
  args: {
    apiKey: v.string(),
  },
  returns: v.union(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      config: v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    // Hash the plain API key
    const hashed = hashApiKey(args.apiKey);

    // Look up machine by hashed key
    const machine = await ctx.db
      .query("machines")
      .withIndex("by_api_key", (q) => q.eq("apiKey", hashed))
      .unique();

    if (!machine) return null;

    return {
      _id: machine._id,
      name: machine.name,
      status: machine.status,
      config: machine.config,
    };
  },
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

    // Update machine
    await ctx.db.patch(args.machineId, {
      lastHeartbeat: now,
      status: newStatus,
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
    const hasAccess = await canAccessMachine(ctx, args.machineId);
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
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Verify machine exists
    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Verify gestionnaire exists and has correct role
    const gestionnaire = await ctx.db.get(args.gestionnaireId);
    if (!gestionnaire) {
      throw new Error("Gestionnaire not found");
    }
    if (gestionnaire.role !== "gestionnaire") {
      throw new Error("Target user is not a gestionnaire");
    }

    // Check permissions - gestionnaire can only manage machines they own
    if (currentUser.role === "gestionnaire") {
      const isManager = await isGestionnaireOfMachine(
        ctx,
        currentUser._id,
        args.machineId,
      );
      if (!isManager) {
        throw new Error("Not authorized to manage this machine");
      }
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
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Check permissions
    if (currentUser.role === "gestionnaire") {
      const isManager = await isGestionnaireOfMachine(
        ctx,
        currentUser._id,
        args.machineId,
      );
      if (!isManager) {
        throw new Error("Not authorized to manage this machine");
      }
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
    const hasAccess = await canAccessMachine(ctx, args.machineId);
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

    if (!targetGestionnaireId) {
      if (currentUser.role === "gestionnaire") {
        targetGestionnaireId = currentUser._id;
      } else if (
        currentUser.role === "technician" &&
        currentUser.gestionnaireId
      ) {
        targetGestionnaireId = currentUser.gestionnaireId;
      } else if (currentUser.role !== "admin") {
        return [];
      }
    }

    // Admin can query any gestionnaire, others only their own
    if (currentUser.role !== "admin") {
      if (currentUser.role === "gestionnaire") {
        if (targetGestionnaireId !== currentUser._id) {
          return [];
        }
      } else if (currentUser.role === "technician") {
        if (targetGestionnaireId !== currentUser.gestionnaireId) {
          return [];
        }
      } else {
        return [];
      }
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
        if (!m) return null;
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
