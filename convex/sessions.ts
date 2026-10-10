/**
 * Sessions, read side.
 *
 * Nothing in this module creates, starts or ends a session. A session is
 * created by `training.launchAutoSession` (an auto session launched from the
 * dashboard) or registered by the machine (`training.registerLocalSession`),
 * and it ends through the training routes.
 *
 * A session with no `kind` belongs to the retired ECG recording mode. It is
 * read-only history and is reported here with the kind "recording".
 */
import { query } from "./_generated/server";
import { v } from "convex/values";
import {
  getCurrentUserOrThrow,
  canAccessMachine,
  canAccessSession,
} from "./lib/auth";

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
      userId: v.optional(v.id("users")),
      startedById: v.optional(v.id("users")),
      status: v.string(),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      channels: v.array(v.string()),
      sampleRate: v.optional(v.number()),
      notes: v.optional(v.string()),
      // null for a session started at the machine with no rider chosen.
      patient: v.union(
        v.object({
          _id: v.id("users"),
          firstName: v.string(),
          lastName: v.string(),
          email: v.string(),
        }),
        v.null(),
      ),
      kind: v.string(), // "recording" | "auto" | "manual"
      subjectLabel: v.optional(v.string()),
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

    // Check access, in the session's organisation: patient can see own,
    // gestionnaire can see their machine's
    if (!(await canAccessSession(ctx, session, currentUser))) {
      return null;
    }

    // Fetch related data
    const patient = session.userId ? await ctx.db.get(session.userId) : null;
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
      sampleRate: session.sampleRate,
      notes: session.notes,
      patient: patient
        ? {
            _id: patient._id,
            firstName: patient.firstName,
            lastName: patient.lastName,
            email: patient.email,
          }
        : null,
      kind: session.kind ?? "recording",
      subjectLabel: session.subjectLabel,
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
      kind: v.string(), // "recording" | "auto" | "manual"
      origin: v.optional(v.string()), // "remote" | "local"
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
    } else if (currentUser.role === "admin") {
      sessions = await ctx.db.query("sessions").order("desc").take(limit);
    } else {
      // Anyone but the Anheart admin lists inside their organisation
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_organization", (q) =>
          q.eq("organizationId", currentUser.organizationId),
        )
        .order("desc")
        .take(limit);
    }

    // Filter by status if not already filtered by index
    if (args.status && !args.machineId) {
      sessions = sessions.filter((s) => s.status === args.status);
    }

    // Filter by access rights: the session's organisation, then its rider
    // or whoever can access its machine
    const accessibleSessions = [];
    for (const session of sessions) {
      if (await canAccessSession(ctx, session, currentUser)) {
        accessibleSessions.push(session);
      }
    }

    // Enrich with names
    const result = [];
    for (const session of accessibleSessions) {
      const patient = session.userId ? await ctx.db.get(session.userId) : null;
      const machine = await ctx.db.get(session.machineId);

      result.push({
        _id: session._id,
        status: session.status,
        startedAt: session.startedAt,
        endedAt: session.endedAt,
        channels: session.channels,
        patientName: patient
          ? `${patient.firstName} ${patient.lastName}`
          : (session.subjectLabel ?? "Unknown"),
        machineName: machine?.name ?? "Unknown",
        kind: session.kind ?? "recording",
        origin: session.origin,
      });
    }

    return result;
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
    const currentUser = await getCurrentUserOrThrow(ctx);
    const hasAccess = await canAccessMachine(ctx, args.machineId, currentUser);
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

    const patient = session.userId ? await ctx.db.get(session.userId) : null;

    return {
      _id: session._id,
      status: session.status,
      startedAt: session.startedAt,
      channels: session.channels,
      patientName: patient
        ? `${patient.firstName} ${patient.lastName}`
        : (session.subjectLabel ?? "Unknown"),
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
  handler: async (ctx) => {
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
      // Anheart admin sees all completed sessions
      sessions = await ctx.db.query("sessions").order("desc").take(100);
    } else {
      // Organisation admin and gestionnaire see, in their organisation,
      // sessions for machines they have access to
      sessions = await ctx.db
        .query("sessions")
        .withIndex("by_organization", (q) =>
          q.eq("organizationId", currentUser.organizationId),
        )
        .order("desc")
        .take(100);
    }

    // Filter to the completed sessions the caller may read
    const readable = [];
    for (const session of sessions) {
      if (
        session.status === "completed" &&
        (await canAccessSession(ctx, session, currentUser))
      ) {
        readable.push(session);
      }
    }
    sessions = readable;

    // Enrich with names
    const result = [];
    for (const session of sessions) {
      const patient = session.userId ? await ctx.db.get(session.userId) : null;
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
          : (session.subjectLabel ?? "Unknown"),
        machineName: machine?.name ?? "Unknown",
      });
    }

    return result;
  },
});
