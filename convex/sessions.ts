import {
  query,
  mutation,
  internalMutation,
  internalQuery,
} from "./_generated/server";
import { v } from "convex/values";
import {
  requireRole,
  getCurrentUserOrThrow,
  canAccessUser,
  canAccessMachine,
  isGestionnaireOfUser,
} from "./lib/auth";
import { internal } from "./_generated/api";

/**
 * Create a new ECG recording session
 */
export const createSession = mutation({
  args: {
    machineId: v.id("machines"),
    userId: v.id("users"),
    channels: v.array(v.string()),
    notes: v.optional(v.string()),
  },
  returns: v.id("sessions"),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Get and validate machine
    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Check machine access
    const machineAccess = await canAccessMachine(ctx, args.machineId);
    if (!machineAccess) {
      throw new Error("Not authorized to use this machine");
    }

    // Check machine status
    if (machine.status === "offline") {
      throw new Error("Machine is offline. Please wait for it to come online.");
    }
    if (machine.status === "in_session") {
      throw new Error("Machine is already in a session");
    }

    // Get and validate patient
    const patient = await ctx.db.get(args.userId);
    if (!patient) {
      throw new Error("Patient not found");
    }
    if (patient.role !== "user") {
      throw new Error("Selected user is not a patient");
    }

    // Check patient access - gestionnaire must manage this patient
    const patientAccess = await canAccessUser(ctx, args.userId);
    if (!patientAccess) {
      throw new Error("Not authorized to create session for this patient");
    }

    // Validate channels
    if (args.channels.length === 0) {
      throw new Error("At least one channel must be selected");
    }

    // Create session
    const sessionId = await ctx.db.insert("sessions", {
      machineId: args.machineId,
      userId: args.userId,
      startedById: currentUser._id,
      status: "pending",
      startedAt: Date.now(),
      channels: args.channels,
      notes: args.notes,
    });

    return sessionId;
  },
});

/**
 * Start a pending session (called by RPi)
 */
export const startSession = internalMutation({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }
    if (session.status !== "pending") {
      throw new Error(`Session is not pending (status: ${session.status})`);
    }

    const now = Date.now();

    // Update session
    await ctx.db.patch(args.sessionId, {
      status: "active",
      startedAt: now, // Update to actual start time
    });

    // Update machine status
    await ctx.db.patch(session.machineId, {
      status: "in_session",
    });

    return null;
  },
});

/**
 * End an active session
 */
export const endSession = mutation({
  args: {
    sessionId: v.id("sessions"),
    notes: v.optional(v.string()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await requireRole(ctx, ["admin", "gestionnaire"]);

    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    // Check access
    const machineAccess = await canAccessMachine(ctx, session.machineId);
    if (!machineAccess) {
      throw new Error("Not authorized to manage this session");
    }

    if (session.status !== "active") {
      throw new Error(`Session is not active (status: ${session.status})`);
    }

    const now = Date.now();

    // Update session
    await ctx.db.patch(args.sessionId, {
      status: "completed",
      endedAt: now,
      notes: args.notes ?? session.notes,
    });

    // Update machine status
    await ctx.db.patch(session.machineId, {
      status: "online",
    });

    // Schedule summary generation
    await ctx.scheduler.runAfter(0, internal.sessionSummaries.generateSummary, {
      sessionId: args.sessionId,
    });

    return null;
  },
});

/**
 * End session internally (from HTTP endpoint)
 */
export const endSessionInternal = internalMutation({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    if (session.status !== "active") {
      throw new Error(`Session is not active (status: ${session.status})`);
    }

    const now = Date.now();

    // Update session
    await ctx.db.patch(args.sessionId, {
      status: "completed",
      endedAt: now,
    });

    // Update machine status
    await ctx.db.patch(session.machineId, {
      status: "online",
    });

    // Schedule summary generation
    await ctx.scheduler.runAfter(0, internal.sessionSummaries.generateSummary, {
      sessionId: args.sessionId,
    });

    return null;
  },
});

/**
 * Mark a session as failed (called when machine goes offline during session)
 */
export const failSession = internalMutation({
  args: {
    sessionId: v.id("sessions"),
    reason: v.string(),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    // Only fail active or pending sessions
    if (session.status !== "active" && session.status !== "pending") {
      return null; // Already completed or failed
    }

    const now = Date.now();

    // Update session
    await ctx.db.patch(args.sessionId, {
      status: "failed",
      endedAt: now,
      notes: session.notes
        ? `${session.notes}\n\nFailure reason: ${args.reason}`
        : `Failure reason: ${args.reason}`,
    });

    // Update machine status
    await ctx.db.patch(session.machineId, {
      status: "offline",
    });

    return null;
  },
});

/**
 * Get session details
 */
export const getSession = query({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(
    v.object({
      _id: v.id("sessions"),
      _creationTime: v.number(),
      machineId: v.id("machines"),
      userId: v.id("users"),
      startedById: v.optional(v.id("users")),
      status: v.string(),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      channels: v.array(v.string()),
      notes: v.optional(v.string()),
      patient: v.object({
        _id: v.id("users"),
        firstName: v.string(),
        lastName: v.string(),
        email: v.string(),
      }),
      machine: v.object({
        _id: v.id("machines"),
        name: v.string(),
      }),
      startedBy: v.optional(
        v.object({
          _id: v.id("users"),
          firstName: v.string(),
          lastName: v.string(),
        }),
      ),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    // Check access: patient can see own, gestionnaire can see their machine's
    const isPatient = currentUser._id === session.userId;
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !canAccessMach) {
      return null;
    }

    // Fetch related data
    const patient = await ctx.db.get(session.userId);
    const machine = await ctx.db.get(session.machineId);
    let startedBy = null;
    if (session.startedById) {
      const starter = await ctx.db.get(session.startedById);
      if (starter) {
        startedBy = {
          _id: starter._id,
          firstName: starter.firstName,
          lastName: starter.lastName,
        };
      }
    }

    return {
      _id: session._id,
      _creationTime: session._creationTime,
      machineId: session.machineId,
      userId: session.userId,
      startedById: session.startedById,
      status: session.status,
      startedAt: session.startedAt,
      endedAt: session.endedAt,
      channels: session.channels,
      notes: session.notes,
      patient: {
        _id: patient!._id,
        firstName: patient!.firstName,
        lastName: patient!.lastName,
        email: patient!.email,
      },
      machine: {
        _id: machine!._id,
        name: machine!.name,
      },
      startedBy: startedBy ?? undefined,
    };
  },
});

/**
 * List sessions with filters
 */
export const listSessions = query({
  args: {
    status: v.optional(
      v.union(
        v.literal("pending"),
        v.literal("active"),
        v.literal("completed"),
        v.literal("failed"),
      ),
    ),
    machineId: v.optional(v.id("machines")),
    userId: v.optional(v.id("users")),
    limit: v.optional(v.number()),
  },
  returns: v.array(
    v.object({
      _id: v.id("sessions"),
      status: v.string(),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      channels: v.array(v.string()),
      patientName: v.string(),
      machineName: v.string(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const limit = args.limit ?? 50;

    let sessions;

    // Query based on filters
    if (args.machineId && args.status) {
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_machine_and_status", (q) =>
          q.eq("machineId", args.machineId!).eq("status", args.status!),
        )
        .order("desc")
        .take(limit);
    } else if (args.machineId) {
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_machine", (q) => q.eq("machineId", args.machineId!))
        .order("desc")
        .take(limit);
    } else if (args.userId) {
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_user", (q) => q.eq("userId", args.userId!))
        .order("desc")
        .take(limit);
    } else {
      sessions = await ctx.db.query("sessions").order("desc").take(limit);
    }

    // Filter by status if not already filtered by index
    if (args.status && !args.machineId) {
      sessions = sessions.filter((s) => s.status === args.status);
    }

    // Filter by access rights
    const accessibleSessions = [];
    for (const session of sessions) {
      if (currentUser.role === "admin") {
        accessibleSessions.push(session);
      } else if (currentUser.role === "user") {
        if (session.userId === currentUser._id) {
          accessibleSessions.push(session);
        }
      } else {
        // Gestionnaire - check if they can access the machine
        const canAccess = await canAccessMachine(ctx, session.machineId);
        if (canAccess) {
          accessibleSessions.push(session);
        }
      }
    }

    // Enrich with names
    const result = [];
    for (const session of accessibleSessions) {
      const patient = await ctx.db.get(session.userId);
      const machine = await ctx.db.get(session.machineId);

      result.push({
        _id: session._id,
        status: session.status,
        startedAt: session.startedAt,
        endedAt: session.endedAt,
        channels: session.channels,
        patientName: patient
          ? `${patient.firstName} ${patient.lastName}`
          : "Unknown",
        machineName: machine?.name ?? "Unknown",
      });
    }

    return result;
  },
});

/**
 * Get pending session for a machine (RPi polling)
 */
export const getPendingSessionForMachine = internalQuery({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.union(
    v.object({
      _id: v.id("sessions"),
      channels: v.array(v.string()),
      userId: v.id("users"),
      config: v.object({
        sampleRate: v.number(),
        batchInterval: v.number(),
      }),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const session = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .first();

    if (!session) return null;

    // Get machine config
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;

    return {
      _id: session._id,
      channels: session.channels,
      userId: session.userId,
      config: {
        sampleRate: machine.config.sampleRate,
        batchInterval: machine.config.batchInterval,
      },
    };
  },
});

/**
 * Get active session for a machine
 */
export const getActiveSessionForMachine = query({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.union(
    v.object({
      _id: v.id("sessions"),
      status: v.string(),
      startedAt: v.number(),
      channels: v.array(v.string()),
      patientName: v.string(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const hasAccess = await canAccessMachine(ctx, args.machineId);
    if (!hasAccess) {
      return null;
    }

    const session = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "active"),
      )
      .first();

    if (!session) return null;

    const patient = await ctx.db.get(session.userId);

    return {
      _id: session._id,
      status: session.status,
      startedAt: session.startedAt,
      channels: session.channels,
      patientName: patient
        ? `${patient.firstName} ${patient.lastName}`
        : "Unknown",
    };
  },
});

/**
 * Get completed sessions for the current user (for reports)
 */
export const getCompletedSessionsForUser = query({
  args: {},
  returns: v.array(
    v.object({
      _id: v.id("sessions"),
      status: v.string(),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      channels: v.array(v.string()),
      notes: v.optional(v.string()),
      patientName: v.string(),
      machineName: v.string(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    let sessions;

    if (currentUser.role === "user") {
      // Patient sees only their own completed sessions
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_user", (q) => q.eq("userId", currentUser._id))
        .order("desc")
        .take(100);
    } else if (currentUser.role === "admin") {
      // Admin sees all completed sessions
      sessions = await ctx.db.query("sessions").order("desc").take(100);
    } else {
      // Gestionnaire sees sessions for machines they have access to
      sessions = await ctx.db.query("sessions").order("desc").take(100);
      const accessibleSessions = [];
      for (const session of sessions) {
        const canAccess = await canAccessMachine(ctx, session.machineId);
        if (canAccess) {
          accessibleSessions.push(session);
        }
      }
      sessions = accessibleSessions;
    }

    // Filter to completed sessions only
    sessions = sessions.filter((s) => s.status === "completed");

    // Enrich with names
    const result = [];
    for (const session of sessions) {
      const patient = await ctx.db.get(session.userId);
      const machine = await ctx.db.get(session.machineId);

      result.push({
        _id: session._id,
        status: session.status,
        startedAt: session.startedAt,
        endedAt: session.endedAt,
        channels: session.channels,
        notes: session.notes,
        patientName: patient
          ? `${patient.firstName} ${patient.lastName}`
          : "Unknown",
        machineName: machine?.name ?? "Unknown",
      });
    }

    return result;
  },
});

/**
 * Cancel a pending session
 */
export const cancelSession = mutation({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await requireRole(ctx, ["admin", "gestionnaire"]);

    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    const hasAccess = await canAccessMachine(ctx, session.machineId);
    if (!hasAccess) {
      throw new Error("Not authorized to manage this session");
    }

    if (session.status !== "pending") {
      throw new Error("Can only cancel pending sessions");
    }

    // Delete the session
    await ctx.db.delete(args.sessionId);

    return null;
  },
});
