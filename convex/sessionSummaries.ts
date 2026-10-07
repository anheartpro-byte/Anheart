/**
 * Session summaries of the retired ECG recording mode: read-only history.
 *
 * A summary was computed from the ECG batches when a recording session ended.
 * Nothing computes or stores one any more: the summary of a training session
 * will come from the session record (ANH-89). The queries below only read the
 * rows that already exist in `session_summaries`.
 */
import { query } from "./_generated/server";
import { v } from "convex/values";
import { getCurrentUserOrThrow, canAccessMachine } from "./lib/auth";

/**
 * Get session summary
 */
export const getSummary = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("session_summaries"),
      sessionId: v.id("sessions"),
      duration: v.number(),
      metrics: v.object({
        avgHeartRate: v.number(),
        minHeartRate: v.number(),
        maxHeartRate: v.number(),
        hrv: v.optional(v.number()),
      }),
      reportFileId: v.optional(v.id("_storage")),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session for access control
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get summary
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    if (!summary) return null;

    return {
      _id: summary._id,
      sessionId: summary.sessionId,
      duration: summary.duration,
      metrics: summary.metrics,
      reportFileId: summary.reportFileId,
      createdAt: summary.createdAt,
    };
  },
});

/**
 * Get summary with downsampled ECG for chart display
 */
export const getSummaryWithEcg = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("session_summaries"),
      sessionId: v.id("sessions"),
      duration: v.number(),
      metrics: v.object({
        avgHeartRate: v.number(),
        minHeartRate: v.number(),
        maxHeartRate: v.number(),
        hrv: v.optional(v.number()),
      }),
      downsampledEcg: v.array(
        v.object({
          timestamp: v.number(),
          value: v.number(),
        }),
      ),
      reportFileId: v.optional(v.id("_storage")),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session for access control
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get summary with ECG data
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    return summary;
  },
});
