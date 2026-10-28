/**
 * Reports of training sessions (ANH-89): the summary of each finished
 * session, and the history of a rider.
 *
 * A summary is computed by an action and written by an internal mutation,
 * twice at most per session:
 *
 * 1. **Provisional**, when the machine reports the end of the session
 *    (`training.endTrainingSession` schedules `summarizeTelemetry`): from the
 *    1 Hz telemetry and the verdict events the machine sent. Marked
 *    `source: "telemetry"`.
 * 2. **Final**, as soon as the three files it reads (`manifest.json`,
 *    `ticks.csv`, `events.jsonl`) of the deposited record are verified
 *    (`recordStorage.finishVerification` calls `scheduleRecordSummary`),
 *    without waiting for the raw blocks: `summarizeRecord`. Marked
 *    `source: "record"`; it replaces the provisional one, and a provisional
 *    one never replaces it.
 *
 * Both are idempotent: computed again from the same rows or the same verified
 * (immutable) files, a summary is the same. The figures and their
 * definitions are `convex/lib/sessionSummary.ts`.
 *
 * A summary is health data. It is read only here, under the rights of its
 * session (`canAccessSession`): the summary of a session is served to whoever
 * may read the session, and to no one else. Nothing of it is logged.
 */
import { v } from "convex/values";
import {
  internalAction,
  internalMutation,
  internalQuery,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { canAccessSession, getCurrentUserOrThrow } from "./lib/auth";
import { MAX_RECORD_FILE_BYTES } from "./lib/recordStorage";
import {
  bounded,
  parseVerdict,
  recordProblemValidator,
  SUMMARY_FILES,
  SummaryBuilder,
  summarizeRecordFiles,
  summaryFiguresValidator,
  summarySourceValidator,
} from "./lib/sessionSummary";
import { readableSessions, stillReadable } from "./lib/readableSessions";
import { ARMING_REFUSAL_CODES } from "./training";

/** Known pre-start endings, duplicated from machineRegistry.ts (unvalidated sessions). */
export function endedBeforeStart(
  session: Pick<Doc<"sessions">, "status" | "endCode" | "endReason">,
): boolean {
  return (
    session.status === "failed" &&
    (ARMING_REFUSAL_CODES.some((code) => code === session.endCode) ||
      (session.endCode === undefined &&
        (session.endReason?.startsWith("Cancelled before start by") === true ||
          session.endReason?.startsWith("refusee par la machine : ") === true)))
  );
}

/** The statuses of a session that is over: only those have a summary. */
const FINISHED: ReadonlySet<string> = new Set(["completed", "failed"]);

/** Telemetry points read per page, and pages at most: six hours at 1 Hz. */
const TELEMETRY_PAGE = 1000;
const MAX_TELEMETRY_PAGES = 22;

/** Events read per page, and pages at most. */
const EVENT_PAGE = 500;
const MAX_EVENT_PAGES = 20;

/** Finished sessions a rider's history lists at most, newest first. */
export const HISTORY_LIMIT = 100;

/** A finished training session (the retired recording mode has no summary). */
function summarizable(
  session: Doc<"sessions"> | null,
): session is Doc<"sessions"> & { kind: "auto" | "manual" } {
  return (
    session !== null &&
    FINISHED.has(session.status) &&
    (session.kind === "auto" || session.kind === "manual")
  );
}

async function summaryOf(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<Doc<"session_summaries_v2"> | null> {
  return await ctx.db
    .query("session_summaries_v2")
    .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
    .unique();
}

/** The storage of the three files a summary reads, once all are verified. */
export async function verifiedSummaryFiles(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<Id<"_storage">[] | null> {
  const found: Id<"_storage">[] = [];
  for (const path of SUMMARY_FILES) {
    const file = await ctx.db
      .query("session_record_files")
      .withIndex("by_session_and_path", (q) =>
        q.eq("sessionId", sessionId).eq("path", path),
      )
      .unique();
    if (!file?.verified) return null;
    found.push(file.storageId);
  }
  return found;
}

/**
 * Schedule the final summary of a session once the three files it reads are
 * verified. Called by `recordStorage.finishVerification` each time one of
 * them is: the last of the three schedules it.
 */
export async function scheduleRecordSummary(
  ctx: MutationCtx,
  sessionId: Id<"sessions">,
): Promise<void> {
  if ((await verifiedSummaryFiles(ctx, sessionId)) === null) return;
  await ctx.scheduler.runAfter(0, internal.sessionReports.summarizeRecord, {
    sessionId,
  });
}

// ---------------------------------------------------------------------------
// Computing
// ---------------------------------------------------------------------------

const zoneValidator = v.object({ lowBpm: v.number(), highBpm: v.number() });

/** What the provisional summary needs of its session; null when it has none to compute. */
export const telemetryInput = internalQuery({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.null(),
    v.object({
      zone: v.union(zoneValidator, v.null()),
      endReason: v.union(v.string(), v.null()),
    }),
  ),
  handler: async (ctx, { sessionId }) => {
    const session = await ctx.db.get(sessionId);
    if (!summarizable(session)) return null;
    if ((await summaryOf(ctx, sessionId))?.source === "record") return null;
    return {
      zone:
        session.kind === "auto" &&
        session.zoneLowBpm !== undefined &&
        session.zoneHighBpm !== undefined
          ? { lowBpm: session.zoneLowBpm, highBpm: session.zoneHighBpm }
          : null,
      endReason:
        session.endReason === undefined ? null : bounded(session.endReason),
    };
  },
});

/** One page of a session's telemetry, oldest first. */
export const telemetryPage = internalQuery({
  args: {
    sessionId: v.id("sessions"),
    cursor: v.union(v.string(), v.null()),
  },
  returns: v.object({
    points: v.array(
      v.object({
        t: v.number(),
        elapsedS: v.number(),
        bpm: v.union(v.number(), v.null()),
        outputRpm: v.number(),
        motorRpm: v.number(),
        gLoad: v.number(),
      }),
    ),
    cursor: v.union(v.string(), v.null()),
  }),
  handler: async (ctx, args) => {
    const page = await ctx.db
      .query("training_telemetry")
      .withIndex("by_session_and_t", (q) => q.eq("sessionId", args.sessionId))
      .paginate({ cursor: args.cursor, numItems: TELEMETRY_PAGE });
    return {
      points: page.page.map((point) => ({
        t: point.t,
        elapsedS: point.elapsedS,
        bpm: point.bpm ?? null,
        outputRpm: point.outputRpm,
        motorRpm: point.motorRpm,
        gLoad: point.gLoad,
      })),
      cursor: page.isDone ? null : page.continueCursor,
    };
  },
});

/** The verdict events of one page of a session's events, in their order. */
export const verdictPage = internalQuery({
  args: {
    sessionId: v.id("sessions"),
    cursor: v.union(v.string(), v.null()),
  },
  returns: v.object({
    verdicts: v.array(v.object({ t: v.number(), detail: v.string() })),
    cursor: v.union(v.string(), v.null()),
  }),
  handler: async (ctx, args) => {
    const page = await ctx.db
      .query("training_events")
      .withIndex("by_session_and_seq", (q) => q.eq("sessionId", args.sessionId))
      .paginate({ cursor: args.cursor, numItems: EVENT_PAGE });
    return {
      verdicts: page.page
        .filter((event) => event.kind === "verdict")
        .map((event) => ({ t: event.t, detail: event.detail })),
      cursor: page.isDone ? null : page.continueCursor,
    };
  },
});

/** The provisional summary of a session, from its telemetry. */
export const summarizeTelemetry = internalAction({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, { sessionId }) => {
    const input = await ctx.runQuery(internal.sessionReports.telemetryInput, {
      sessionId,
    });
    if (input === null) return null;
    const builder = new SummaryBuilder(input.zone, false);
    const warnings: string[] = [];
    // `t = 0` of the session on the machine's axis, where its points and its
    // events are dated: each point carries its date and its elapsed time.
    let origin: number | null = null;
    let cursor: string | null = null;
    let pages = 0;
    do {
      if (pages === MAX_TELEMETRY_PAGES) {
        warnings.push("telemetry_truncated");
        break;
      }
      pages++;
      const page: { points: Point[]; cursor: string | null } =
        await ctx.runQuery(internal.sessionReports.telemetryPage, {
          sessionId,
          cursor,
        });
      for (const point of page.points) {
        origin ??= point.t - point.elapsedS * 1000;
        builder.add({
          t: point.elapsedS,
          bpm: point.bpm,
          outputRpm: point.outputRpm,
          motorRpm: point.motorRpm,
          gReference: point.gLoad,
          gLegTip: null,
          rule: null,
          action: null,
          state: null,
          judged: false,
        });
      }
      cursor = page.cursor;
    } while (cursor !== null);
    cursor = null;
    pages = 0;
    do {
      pages++;
      const page: { verdicts: VerdictEvent[]; cursor: string | null } =
        await ctx.runQuery(internal.sessionReports.verdictPage, {
          sessionId,
          cursor,
        });
      for (const event of page.verdicts) {
        const verdict = parseVerdict(event.detail);
        if (verdict !== null)
          builder.verdict(
            verdict.rule,
            verdict.action,
            origin === null ? null : (event.t - origin) / 1000,
          );
      }
      cursor = page.cursor;
    } while (cursor !== null && pages < MAX_EVENT_PAGES);
    if (cursor !== null) warnings.push("events_truncated");
    await ctx.runMutation(internal.sessionReports.storeSummary, {
      sessionId,
      source: "telemetry",
      figures: builder.figures({
        endReason: input.endReason,
        referenceRadiusM: null,
        legTipRadiusM: null,
        legTipMeasured: null,
        warnings,
      }),
    });
    return null;
  },
});

type Point = {
  t: number;
  elapsedS: number;
  bpm: number | null;
  outputRpm: number;
  motorRpm: number;
  gLoad: number;
};
type VerdictEvent = { t: number; detail: string };

/** The three verified files of a session's record; null when not all are. */
export const recordInput = internalQuery({
  args: { sessionId: v.id("sessions") },
  returns: v.union(v.null(), v.array(v.id("_storage"))),
  handler: async (ctx, { sessionId }) => {
    const session = await ctx.db.get(sessionId);
    if (!summarizable(session) || session.recordDeleted) return null;
    return await verifiedSummaryFiles(ctx, sessionId);
  },
});

/** The final summary of a session, from its deposited record. */
export const summarizeRecord = internalAction({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, { sessionId }) => {
    const files = await ctx.runQuery(internal.sessionReports.recordInput, {
      sessionId,
    });
    if (files === null) return null;
    try {
      const outcome = await summarizeRecordFiles({
        manifest: () => ctx.storage.get(files[0]),
        ticks: () => ctx.storage.get(files[1]),
        events: () => ctx.storage.get(files[2]),
        maxFileBytes: MAX_RECORD_FILE_BYTES,
      });
      if ("problem" in outcome)
        await ctx.runMutation(internal.sessionReports.noteRecordProblem, {
          sessionId,
          problem: outcome.problem,
        });
      else
        await ctx.runMutation(internal.sessionReports.storeSummary, {
          sessionId,
          source: "record",
          figures: outcome.figures,
        });
    } catch {
      console.warn({ sessionId, code: "summary_failed" });
      await ctx.runMutation(internal.sessionReports.noteRecordProblem, {
        sessionId,
        problem: "summary_failed",
      });
    }
    return null;
  },
});

/**
 * Write the summary of a finished session. The summary of the record
 * replaces whatever was there; the one of the telemetry never replaces the
 * summary of the record.
 */
export const storeSummary = internalMutation({
  args: {
    sessionId: v.id("sessions"),
    source: summarySourceValidator,
    figures: summaryFiguresValidator,
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!summarizable(session)) return null;
    const existing = await summaryOf(ctx, args.sessionId);
    if (existing?.source === "record" && args.source === "telemetry")
      return null;
    const row = {
      organizationId: session.organizationId,
      sessionId: args.sessionId,
      source: args.source,
      computedAt: Date.now(),
      figures: args.figures,
    };
    if (existing) await ctx.db.replace(existing._id, row);
    else await ctx.db.insert("session_summaries_v2", row);
    return null;
  },
});

/** The deposited record could not be summarised: the provisional summary says why. */
export const noteRecordProblem = internalMutation({
  args: { sessionId: v.id("sessions"), problem: recordProblemValidator },
  returns: v.null(),
  handler: async (ctx, args) => {
    const existing = await summaryOf(ctx, args.sessionId);
    if (existing?.source === "telemetry")
      await ctx.db.patch(existing._id, { recordProblem: args.problem });
    return null;
  },
});

// ---------------------------------------------------------------------------
// Reading
// ---------------------------------------------------------------------------

/**
 * The summary of a session, for its page and its report. Null for a session
 * the caller may not read (`canAccessSession`), exactly as for one that does
 * not exist. Only a reader learns why a summary is absent.
 */
export const getSummary = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.null(),
    v.object({
      absent: v.union(v.literal("not_started"), v.literal("not_computed")),
    }),
    v.object({
      provisional: v.boolean(),
      computedAt: v.number(),
      recordProblem: v.union(v.string(), v.null()),
      figures: summaryFiguresValidator,
    }),
  ),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    const session = await ctx.db.get(args.sessionId);
    if (!session || !(await canAccessSession(ctx, session, me))) return null;
    const summary = await summaryOf(ctx, session._id);
    if (!summary)
      return {
        absent:
          summarizable(session) && endedBeforeStart(session)
            ? ("not_started" as const)
            : ("not_computed" as const),
      };
    return {
      provisional: summary.source === "telemetry",
      computedAt: summary.computedAt,
      recordProblem: summary.recordProblem ?? null,
      figures: summary.figures,
    };
  },
});

const nullableNumber = v.union(v.number(), v.null());

/** What a history shows of a summary. */
function keyFigures(summary: Doc<"session_summaries_v2">) {
  const { durationS, zone, heartRate, reference } = summary.figures;
  return {
    provisional: summary.source === "telemetry",
    durationS,
    inZoneS: zone?.inZoneS ?? null,
    zoneLowBpm: zone?.lowBpm ?? null,
    zoneHighBpm: zone?.highBpm ?? null,
    meanBpm: heartRate?.meanBpm ?? null,
    maxBpm: heartRate?.maxBpm ?? null,
    maxGReference: reference.maxG,
  };
}

/**
 * The finished training sessions of a rider the caller may read, newest
 * first, `HISTORY_LIMIT` at most, each with the key figures of its summary:
 * the rider's history and progression. The sessions are those of
 * `sessions.listSessions` for this rider (`readableSessions`, then
 * `canAccessSession` on each): a gestionnaire reads those on the machines
 * they manage, a rider their own, another organisation nothing. Each row
 * also carries the rider's nausea score of `session_feedback` (ANH-146), or
 * null without feedback, under the same rights.
 */
export const riderHistory = query({
  args: { userId: v.id("users") },
  returns: v.array(
    v.object({
      sessionId: v.id("sessions"),
      status: v.string(),
      kind: v.string(),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      machineName: v.string(),
      profileName: v.optional(v.string()),
      notStarted: v.boolean(),
      summary: v.union(
        v.null(),
        v.object({
          provisional: v.boolean(),
          durationS: nullableNumber,
          inZoneS: nullableNumber,
          zoneLowBpm: nullableNumber,
          zoneHighBpm: nullableNumber,
          meanBpm: nullableNumber,
          maxBpm: nullableNumber,
          maxGReference: nullableNumber,
        }),
      ),
      nauseaScore: nullableNumber,
    }),
  ),
  handler: async (ctx, { userId }) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const finished: Doc<"sessions">[] = [];
    for (const status of ["completed", "failed"] as const) {
      const sessions = await readableSessions(ctx, caller, { userId, status });
      finished.push(...(await sessions.take(HISTORY_LIMIT)));
    }
    finished.sort(
      (a, b) => b.startedAt - a.startedAt || b._creationTime - a._creationTime,
    );
    const readable = await stillReadable(
      ctx,
      caller,
      finished.slice(0, HISTORY_LIMIT),
    );
    const rows = [];
    for (const session of readable) {
      if (!summarizable(session)) continue;
      const machine = await ctx.db.get(session.machineId);
      const summary = await summaryOf(ctx, session._id);
      const feedback = await ctx.db
        .query("session_feedback")
        .withIndex("by_session", (q) => q.eq("sessionId", session._id))
        .unique();
      rows.push({
        sessionId: session._id,
        status: session.status,
        kind: session.kind,
        startedAt: session.startedAt,
        endedAt: session.endedAt,
        machineName: machine?.name ?? "-",
        profileName: session.profileName,
        notStarted: summary === null && endedBeforeStart(session),
        summary: summary === null ? null : keyFigures(summary),
        nauseaScore: feedback?.nauseaScore ?? null,
      });
    }
    return rows;
  },
});
