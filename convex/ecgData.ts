import { query, internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";
import { getCurrentUserOrThrow, canAccessMachine } from "./lib/auth";

// ============================================
// Internal Functions (for HTTP endpoints)
// ============================================

/**
 * Store a batch of ECG data from RPi
 */
export const storeEcgBatch = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    timestamp: v.number(),
    samples: v.array(
      v.object({
        channel: v.string(),
        values: v.array(v.number()),
      }),
    ),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    // Validate session exists
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    // Validate session is active
    if (session.status !== "active") {
      throw new Error(`Session is not active (status: ${session.status})`);
    }

    // Validate session belongs to this machine
    if (session.machineId !== args.machineId) {
      throw new Error("Session does not belong to this machine");
    }

    // Store the data batch
    await ctx.db.insert("ecg_data", {
      sessionId: args.sessionId,
      timestamp: args.timestamp,
      samples: args.samples,
    });

    return null;
  },
});

/**
 * Get data count for a session (for debugging)
 */
export const getSessionDataCount = internalQuery({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.number(),
  handler: async (ctx, args) => {
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();
    return data.length;
  },
});

/**
 * Get all ECG data for a session (for summary generation)
 */
export const getAllSessionData = internalQuery({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.array(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
  ),
  handler: async (ctx, args) => {
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();

    return data.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});

// ============================================
// Public Queries (Task 3.3)
// ============================================

/**
 * Get recent ECG data for live viewing
 * Includes 5-second delay for gestionnaire/technician
 */
export const getRecentEcgData = query({
  args: {
    sessionId: v.id("sessions"),
    seconds: v.optional(v.number()),
  },
  returns: v.array(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const seconds = args.seconds ?? 10;
    const now = Date.now();

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return [];
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return [];
    }

    // Determine delay
    // No delay for: patient, admin, or completed sessions
    // 5s delay for: gestionnaire, technician viewing active sessions
    let delay = 0;
    if (!isPatient && !isAdmin && session.status === "active") {
      delay = 5000; // 5 seconds
    }

    // Calculate time window
    const maxTime = now - delay;
    const minTime = maxTime - seconds * 1000;

    // Query data
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId).gt("timestamp", minTime),
      )
      .collect();

    // Filter by max time (for delay)
    const filteredData = data.filter((d) => d.timestamp <= maxTime);

    // Sort by timestamp ascending
    filteredData.sort((a, b) => a.timestamp - b.timestamp);

    return filteredData.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});

/**
 * Get ECG data for a time range (historical view)
 */
export const getSessionEcgRange = query({
  args: {
    sessionId: v.id("sessions"),
    startTime: v.number(),
    endTime: v.number(),
  },
  returns: v.array(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return [];
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return [];
    }

    // Query data in range
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q
          .eq("sessionId", args.sessionId)
          .gt("timestamp", args.startTime)
          .lt("timestamp", args.endTime),
      )
      .collect();

    // Sort by timestamp ascending
    data.sort((a, b) => a.timestamp - b.timestamp);

    return data.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});

/**
 * Get session data stats (for UI display)
 */
export const getSessionDataStats = query({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(
    v.object({
      totalBatches: v.number(),
      firstTimestamp: v.optional(v.number()),
      lastTimestamp: v.optional(v.number()),
      channels: v.array(v.string()),
      durationSeconds: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return null;
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get all data (consider pagination for very long sessions)
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();

    if (data.length === 0) {
      return {
        totalBatches: 0,
        firstTimestamp: undefined,
        lastTimestamp: undefined,
        channels: session.channels,
        durationSeconds: 0,
      };
    }

    // Sort to find first and last
    data.sort((a, b) => a.timestamp - b.timestamp);
    const firstTimestamp = data[0].timestamp;
    const lastTimestamp = data[data.length - 1].timestamp;
    const durationSeconds = Math.round((lastTimestamp - firstTimestamp) / 1000);

    // Get unique channels from actual data
    const channelsSet = new Set<string>();
    for (const batch of data) {
      for (const sample of batch.samples) {
        channelsSet.add(sample.channel);
      }
    }

    return {
      totalBatches: data.length,
      firstTimestamp,
      lastTimestamp,
      channels: Array.from(channelsSet),
      durationSeconds,
    };
  },
});

/**
 * Subscribe to latest ECG data (for real-time updates)
 * Returns the most recent batch only
 */
export const getLatestEcgBatch = query({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const now = Date.now();

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return null;
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Determine delay
    let delay = 0;
    if (!isPatient && !isAdmin && session.status === "active") {
      delay = 5000;
    }

    const maxTime = now - delay;

    // Get latest batch (within delay window)
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .order("desc")
      .first();

    if (!data || data.timestamp > maxTime) {
      return null;
    }

    return {
      timestamp: data.timestamp,
      samples: data.samples,
    };
  },
});
