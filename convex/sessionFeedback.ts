/**
 * The rider's feedback after a training session (ANH-146): a nausea score, a
 * discomfort or dizziness score, a comfort and an optional free text, one row
 * of `session_feedback` per session at most. The scale is
 * `contracts/feedback.json` (`convex/lib/feedback.ts`).
 *
 * - Who writes: the session's rider from the site (`submitSessionFeedback`,
 *   `submittedBy: "rider"`), or the operator from the machine's console
 *   (`storeMachineFeedback`, through the `feedback` event of
 *   `POST /api/machine/training/events`, `submittedBy: "operator"`). The last
 *   write wins, whoever made it.
 * - When: once the session is over (`completed` or `failed`, with an end), and
 *   for `EDIT_WINDOW_MS` after its end, on the server's clock.
 * - Which sessions: a training session with someone on board
 *   (`personOnBoard`); never a bench session or one of the retired recording
 *   mode.
 * - Who reads: whoever may read the session (`canAccessSession`); the export
 *   is for organisation admins, with identifiers only and no free text.
 *
 * Health data: it goes to `session_feedback` and nowhere else. No refusal,
 * audit entry or log names a value.
 */
import {
  paginationOptsValidator,
  paginationResultValidator,
} from "convex/server";
import { v } from "convex/values";
import {
  internalMutation,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import {
  canAccessSession,
  getCurrentUserOrThrow,
  ORGANIZATION_ADMIN_ROLES,
  requireRole,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";
import { audit, machineActor, userActor } from "./lib/audit";
import { machineError } from "./lib/contract";
import { refusal } from "./lib/errors";
import {
  EDIT_WINDOW_MS,
  feedbackComfortValidator,
  feedbackSubmitterValidator,
  validateFeedback,
  type Feedback,
  type FeedbackSubmitter,
} from "./lib/feedback";
import { personOnBoard } from "./lib/machineRegistry";
import { limitUser } from "./lib/rateLimits";

/** Rows of the export served per page at most. */
export const EXPORT_PAGE_MAX = 100;

const nullableNumber = v.union(v.number(), v.null());

const feedbackValidator = v.object({
  nauseaScore: v.number(),
  dizzinessScore: v.number(),
  comfort: feedbackComfortValidator,
  freeText: v.optional(v.string()),
  submittedBy: feedbackSubmitterValidator,
  submittedAt: v.number(),
});

/** A training session that had someone on board: the only kind with feedback. */
function eligible(session: Doc<"sessions">): boolean {
  return session.kind !== undefined && personOnBoard(session);
}

/** The end of the feedback window of a finished session; null before its end. */
function editableUntil(session: Doc<"sessions">): number | null {
  const over = session.status === "completed" || session.status === "failed";
  return over && session.endedAt !== undefined
    ? session.endedAt + EDIT_WINDOW_MS
    : null;
}

/** The session's rider, in the session's organisation. */
function isRider(session: Doc<"sessions">, caller: CurrentUser): boolean {
  return (
    session.userId === caller._id &&
    sameOrganization(caller, session.organizationId)
  );
}

async function feedbackOf(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<Doc<"session_feedback"> | null> {
  return await ctx.db
    .query("session_feedback")
    .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
    .unique();
}

/**
 * Why a session takes no feedback, as codes shared by the site and the
 * machine API, with the words the machine reads: in English, without a value.
 */
const MACHINE_REFUSALS = {
  feedback_not_eligible: "The session had no one on board: no feedback",
  feedback_session_not_ended: "The session is not over: no feedback yet",
  feedback_window_closed: "The feedback window of the session is closed",
} as const;

/** Why `session` takes no feedback now; null when it does. */
function closedBecause(
  session: Doc<"sessions">,
): keyof typeof MACHINE_REFUSALS | null {
  if (!eligible(session)) return "feedback_not_eligible";
  const until = editableUntil(session);
  if (until === null) return "feedback_session_not_ended";
  return Date.now() > until ? "feedback_window_closed" : null;
}

/** Write the one row of the session: replaced whole, so a text left out is gone. */
async function upsert(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  feedback: Feedback,
  submittedBy: FeedbackSubmitter,
): Promise<void> {
  const row = {
    organizationId: session.organizationId,
    sessionId: session._id,
    ...feedback,
    submittedBy,
    submittedAt: Date.now(),
  };
  const existing = await feedbackOf(ctx, session._id);
  if (existing) await ctx.db.replace(existing._id, row);
  else await ctx.db.insert("session_feedback", row);
}

/**
 * The feedback of a session, and whether the caller may give or change it.
 * Null for a session the caller may not read (`canAccessSession`), exactly
 * as for one that does not exist. `canEdit` is true for the session's rider
 * on a finished session that takes feedback; the site compares
 * `editableUntil` with the time, and `submitSessionFeedback` decides.
 */
export const getSessionFeedback = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.null(),
    v.object({
      eligible: v.boolean(),
      feedback: v.union(v.null(), feedbackValidator),
      endedAt: nullableNumber,
      editableUntil: nullableNumber,
      canEdit: v.boolean(),
    }),
  ),
  handler: async (ctx, { sessionId }) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const session = await ctx.db.get(sessionId);
    if (!session || !(await canAccessSession(ctx, session, caller)))
      return null;
    const row = await feedbackOf(ctx, sessionId);
    const until = eligible(session) ? editableUntil(session) : null;
    return {
      eligible: eligible(session),
      feedback:
        row === null
          ? null
          : {
              nauseaScore: row.nauseaScore,
              dizzinessScore: row.dizzinessScore,
              comfort: row.comfort,
              freeText: row.freeText,
              submittedBy: row.submittedBy,
              submittedAt: row.submittedAt,
            },
      endedAt: session.endedAt ?? null,
      editableUntil: until,
      canEdit: until !== null && isRider(session, caller),
    };
  },
});

/**
 * The rider gives or changes the feedback of their session, from the site.
 * Refused with a stable code, never naming a value: `session_not_found` for
 * a session the caller may not read, `feedback_rider_only` for anyone but its
 * rider, then `feedback_not_eligible`, `feedback_session_not_ended`,
 * `feedback_window_closed`, and `feedback_invalid` for a value off the scale.
 */
export const submitSessionFeedback = mutation({
  args: {
    sessionId: v.id("sessions"),
    // Every value entered is loose here, whatever its type, and checked below
    // by validateFeedback: a validator would quote a wrong value back, in the
    // refusal and in the deployment's log.
    nauseaScore: v.any(),
    dizzinessScore: v.any(),
    comfort: v.any(),
    freeText: v.optional(v.any()),
  },
  returns: v.null(),
  handler: async (ctx, { sessionId, ...input }) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    const session = await ctx.db.get(sessionId);
    if (!session || !(await canAccessSession(ctx, session, caller)))
      throw refusal("session_not_found");
    if (!isRider(session, caller)) throw refusal("feedback_rider_only");
    const closed = closedBecause(session);
    if (closed !== null) throw refusal(closed);
    const feedback = validateFeedback(input);
    if (feedback === null) throw refusal("feedback_invalid");
    await upsert(ctx, session, feedback, "rider");
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: session.organizationId,
      action: "session.feedback_submitted",
      targetId: session._id,
    });
    return null;
  },
});

/**
 * The feedback the operator entered at the machine, from the `feedback` event
 * of `POST /api/machine/training/events` (contract 1.7). Refused with a code
 * of the machine API, never naming a value: `session_not_found` for a session
 * that is not this machine's, `invalid_request` for a value off the scale,
 * then the codes of `closedBecause`.
 */
export const storeMachineFeedback = internalMutation({
  args: {
    machineId: v.id("machines"),
    // As the machine sent it: an identifier that is not one is not found.
    sessionId: v.string(),
    feedback: v.object({
      nauseaScore: v.number(),
      dizzinessScore: v.number(),
      comfort: v.string(),
      freeText: v.optional(v.string()),
    }),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const sessionId = ctx.db.normalizeId("sessions", args.sessionId);
    const session = sessionId === null ? null : await ctx.db.get(sessionId);
    if (!session || session.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    const feedback = validateFeedback(args.feedback);
    if (feedback === null)
      throw machineError("invalid_request", "Malformed feedback");
    const closed = closedBecause(session);
    if (closed !== null) throw machineError(closed, MACHINE_REFUSALS[closed]);
    await upsert(ctx, session, feedback, "operator");
    await audit(ctx, {
      actor: machineActor(args.machineId),
      source: "machine",
      organizationId: session.organizationId,
      action: "session.feedback_submitted",
      targetId: session._id,
    });
    return null;
  },
});

const exportRowValidator = v.object({
  sessionId: v.id("sessions"),
  organizationId: v.union(v.id("organizations"), v.null()),
  machineId: v.id("machines"),
  userId: v.union(v.id("users"), v.null()),
  startedAt: v.number(),
  durationS: nullableNumber,
  legTipMaxG: nullableNumber,
  legTipMaxGRate: nullableNumber,
  nauseaScore: v.number(),
  dizzinessScore: v.number(),
  comfort: feedbackComfortValidator,
  submittedBy: feedbackSubmitterValidator,
  submittedAt: v.number(),
});

/**
 * The feedback of the sessions, newest first, page by page, crossed with what
 * the session's summary says of it (duration, peak g and peak rate of change
 * of g at the leg tip, null without a summary). Identifiers only: no name,
 * no e-mail address, no free text. The Anheart admin reads every
 * organisation, or the one asked for; an organisation admin reads their own,
 * and nothing when another is asked for. Anyone else is refused.
 */
export const exportFeedback = query({
  args: {
    paginationOpts: paginationOptsValidator,
    organizationId: v.optional(v.id("organizations")),
  },
  returns: paginationResultValidator(exportRowValidator),
  handler: async (ctx, { paginationOpts, organizationId }) => {
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    if (
      caller.role !== "admin" &&
      organizationId !== undefined &&
      organizationId !== caller.organizationId
    )
      return { page: [], isDone: true, continueCursor: "" };
    const scope =
      caller.role === "admin" ? organizationId : caller.organizationId;
    const rows =
      scope === undefined
        ? ctx.db.query("session_feedback").withIndex("by_submittedAt")
        : ctx.db
            .query("session_feedback")
            .withIndex("by_organization_and_submittedAt", (q) =>
              q.eq("organizationId", scope),
            );
    const result = await rows.order("desc").paginate({
      ...paginationOpts,
      numItems: Math.max(1, Math.min(paginationOpts.numItems, EXPORT_PAGE_MAX)),
    });
    const page = [];
    for (const row of result.page) {
      const session = await ctx.db.get(row.sessionId);
      // A session is never deleted; a row without one has nothing to cross.
      if (session === null) continue;
      const summary = await ctx.db
        .query("session_summaries_v2")
        .withIndex("by_session", (q) => q.eq("sessionId", row.sessionId))
        .unique();
      page.push({
        sessionId: row.sessionId,
        organizationId: row.organizationId ?? null,
        machineId: session.machineId,
        userId: session.userId ?? null,
        startedAt: session.startedAt,
        durationS: summary?.figures.durationS ?? null,
        legTipMaxG: summary?.figures.legTip.maxG ?? null,
        legTipMaxGRate: summary?.figures.legTip.maxGRate ?? null,
        nauseaScore: row.nauseaScore,
        dizzinessScore: row.dizzinessScore,
        comfort: row.comfort,
        submittedBy: row.submittedBy,
        submittedAt: row.submittedAt,
      });
    }
    return { ...result, page };
  },
});
