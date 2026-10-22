/**
 * Run once after ANH-82 is deployed, by internal CLI invocation only.
 * Each call reads one page, then schedules its continuation after that page's
 * work. No cron or application function starts this backfill.
 */
import { v } from "convex/values";
import { internal } from "./_generated/api";
import { internalMutation } from "./_generated/server";
import { endedBeforeStart, verifiedSummaryFiles } from "./sessionReports";

export const DEFAULT_BATCH_SIZE = 25;
export const MAX_BATCH_SIZE = 100;
/** Sessions start one second apart; the record follows telemetry by half a second. */
export const SESSION_DELAY_MS = 1000;

export const backfillSummaries = internalMutation({
  args: {
    status: v.optional(v.union(v.literal("completed"), v.literal("failed"))),
    cursor: v.optional(v.union(v.string(), v.null())),
    batchSize: v.optional(v.number()),
  },
  returns: v.object({ scheduled: v.number(), done: v.boolean() }),
  handler: async (ctx, args) => {
    const status = args.status ?? "completed";
    const batchSize = Number.isFinite(args.batchSize ?? DEFAULT_BATCH_SIZE)
      ? Math.max(
          1,
          Math.min(
            MAX_BATCH_SIZE,
            Math.floor(args.batchSize ?? DEFAULT_BATCH_SIZE),
          ),
        )
      : DEFAULT_BATCH_SIZE;
    const page = await ctx.db
      .query("sessions")
      .withIndex("by_status_and_started", (q) => q.eq("status", status))
      .paginate({ cursor: args.cursor ?? null, numItems: batchSize });
    let scheduled = 0;
    for (const [index, session] of page.page.entries()) {
      if (
        (session.kind !== "auto" && session.kind !== "manual") ||
        endedBeforeStart(session)
      )
        continue;
      const summary = await ctx.db
        .query("session_summaries_v2")
        .withIndex("by_session", (q) => q.eq("sessionId", session._id))
        .unique();
      if (summary?.source === "record") continue;
      const delay = index * SESSION_DELAY_MS;
      if (summary === null) {
        await ctx.scheduler.runAfter(
          delay,
          internal.sessionReports.summarizeTelemetry,
          {
            sessionId: session._id,
          },
        );
        scheduled++;
      }
      if (
        !summary?.recordProblem &&
        !session.recordDeleted &&
        (await verifiedSummaryFiles(ctx, session._id)) !== null
      ) {
        await ctx.scheduler.runAfter(
          delay + SESSION_DELAY_MS / 2,
          internal.sessionReports.summarizeRecord,
          {
            sessionId: session._id,
          },
        );
        scheduled++;
      }
    }
    const done = page.isDone && status === "failed";
    if (!done)
      await ctx.scheduler.runAfter(
        (page.page.length + 1) * SESSION_DELAY_MS,
        internal.sessionReportsBackfill.backfillSummaries,
        {
          status: page.isDone ? "failed" : status,
          cursor: page.isDone ? null : page.continueCursor,
          batchSize,
        },
      );
    return { scheduled, done };
  },
});
