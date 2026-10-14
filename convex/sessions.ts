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
 *
 * Every list of sessions (`listSessions`, `listSessionsPaginated`,
 * `getCompletedSessionsForUser`) reads `readableSessions`: the caller's rights
 * are part of the query, so a limit or a page counts only sessions the caller
 * may read (ANH-158).
 */
import { query, type QueryCtx } from "./_generated/server";
import {
  paginationOptsValidator,
  paginationResultValidator,
  type Expression,
} from "convex/server";
import { v, type Infer } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import {
  getCurrentUserOrThrow,
  canAccessMachine,
  canAccessSession,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";

const sessionStatusValidator = v.union(
  v.literal("pending"),
  v.literal("active"),
  // No news of it for a while, its end unknown (`training.interruptSilentSessions`).
  v.literal("interrupted"),
  v.literal("completed"),
  v.literal("failed"),
);

/** What a list can be narrowed to, inside what the caller may read. */
const listFilters = {
  status: v.optional(sessionStatusValidator),
  machineId: v.optional(v.id("machines")),
  userId: v.optional(v.id("users")),
};

type ListFilters = {
  status?: Infer<typeof sessionStatusValidator>;
  machineId?: Id<"machines">;
  userId?: Id<"users">;
};

/** One row of a list of sessions. */
const listedSessionValidator = v.object({
  _id: v.id("sessions"),
  status: v.string(),
  startedAt: v.number(),
  endedAt: v.optional(v.number()),
  channels: v.array(v.string()),
  patientName: v.string(),
  machineName: v.string(),
  kind: v.string(), // "recording" | "auto" | "manual"
  origin: v.optional(v.string()), // "remote" | "local"
});

/**
 * The machines whose sessions a gestionnaire reads through `canAccessMachine`:
 * those linked to them in `machine_gestionnaires` that belong to the
 * organisation of the call.
 */
async function machinesManagedBy(
  ctx: QueryCtx,
  caller: CurrentUser,
): Promise<Id<"machines">[]> {
  const links = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", caller._id))
    .collect();
  const machines = new Set<Id<"machines">>();
  for (const link of links) {
    const machine = await ctx.db.get(link.machineId);
    if (machine && sameOrganization(caller, machine.organizationId)) {
      machines.add(machine._id);
    }
  }
  return [...machines];
}

/**
 * The index range a list reads: the narrowest one the filters and the role
 * give, always ordered by `startedAt`. `statusInIndex` says whether the range
 * already holds the requested status only.
 */
function rangeOf(ctx: QueryCtx, caller: CurrentUser, filters: ListFilters) {
  const { status, machineId, userId } = filters;
  const sessions = ctx.db.query("sessions");
  if (machineId !== undefined) {
    return {
      range: sessions.withIndex("by_machine_and_started", (q) =>
        q.eq("machineId", machineId),
      ),
      statusInIndex: false,
    };
  }
  if (userId !== undefined || caller.role === "user") {
    const rider = userId ?? caller._id;
    return {
      range: sessions.withIndex("by_user_and_started", (q) =>
        q.eq("userId", rider),
      ),
      statusInIndex: false,
    };
  }
  if (caller.role === "admin") {
    return status === undefined
      ? { range: sessions.withIndex("by_started"), statusInIndex: false }
      : {
          range: sessions.withIndex("by_status_and_started", (q) =>
            q.eq("status", status),
          ),
          statusInIndex: true,
        };
  }
  // An organisation admin or a gestionnaire: their organisation.
  return status === undefined
    ? {
        range: sessions.withIndex("by_organization_and_started", (q) =>
          q.eq("organizationId", caller.organizationId),
        ),
        statusInIndex: false,
      }
    : {
        range: sessions.withIndex(
          "by_organization_and_status_and_started",
          (q) =>
            q.eq("organizationId", caller.organizationId).eq("status", status),
        ),
        statusInIndex: true,
      };
}

/**
 * The sessions the caller may read that match the filters, most recent start
 * first (`startedAt`, then creation, both descending).
 *
 * One range of one index (`rangeOf`), then the rule of `canAccessSession`
 * written as a filter of the same query: the Anheart admin reads everything;
 * anyone else reads sessions of their organisation only, an organisation
 * admin all of them, a gestionnaire those they ride and those of the machines
 * they manage there, a user those they ride. A `take` or a `paginate` on the
 * result therefore counts readable sessions only.
 */
async function readableSessions(
  ctx: QueryCtx,
  caller: CurrentUser,
  filters: ListFilters,
) {
  const { range, statusInIndex } = rangeOf(ctx, caller, filters);
  const { status } = filters;
  const machines =
    caller.role === "gestionnaire" ? await machinesManagedBy(ctx, caller) : [];
  return range.order("desc").filter((q) => {
    const conditions: Expression<boolean>[] = [];
    if (status !== undefined && !statusInIndex) {
      conditions.push(q.eq(q.field("status"), status));
    }
    if (caller.role !== "admin") {
      conditions.push(q.eq(q.field("organizationId"), caller.organizationId));
    }
    if (caller.role === "user") {
      conditions.push(q.eq(q.field("userId"), caller._id));
    }
    if (caller.role === "gestionnaire") {
      conditions.push(
        q.or(
          q.eq(q.field("userId"), caller._id),
          ...machines.map((machineId) => q.eq(q.field("machineId"), machineId)),
        ),
      );
    }
    return conditions.length === 0 ? true : q.and(...conditions);
  });
}

/**
 * The sessions of a list that `canAccessSession`, the rule of every other read
 * of a session, still lets the caller read. The filter of `readableSessions`
 * is that rule, so this removes nothing while the two agree; if they ever
 * part, a session is left out rather than shown.
 */
async function stillReadable(
  ctx: QueryCtx,
  caller: CurrentUser,
  sessions: Doc<"sessions">[],
): Promise<Doc<"sessions">[]> {
  const kept = [];
  for (const session of sessions) {
    if (await canAccessSession(ctx, session, caller)) kept.push(session);
  }
  return kept;
}

/** The rider's name (or the label given at the machine) and the machine's. */
async function namesOf(ctx: QueryCtx, session: Doc<"sessions">) {
  const patient = session.userId ? await ctx.db.get(session.userId) : null;
  const machine = await ctx.db.get(session.machineId);
  return {
    patientName: patient
      ? `${patient.firstName} ${patient.lastName}`
      : (session.subjectLabel ?? "Unknown"),
    machineName: machine?.name ?? "Unknown",
  };
}

/** The rows of a list of sessions, named. */
async function listedRows(
  ctx: QueryCtx,
  caller: CurrentUser,
  sessions: Doc<"sessions">[],
) {
  const rows = [];
  for (const session of await stillReadable(ctx, caller, sessions)) {
    rows.push({
      _id: session._id,
      status: session.status,
      startedAt: session.startedAt,
      endedAt: session.endedAt,
      channels: session.channels,
      ...(await namesOf(ctx, session)),
      kind: session.kind ?? "recording",
      origin: session.origin,
    });
  }
  return rows;
}

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
 * The `limit` (50 by default) most recent sessions the caller may read,
 * narrowed by the filters. For the short lists of the site (home page, a
 * patient's page); the Sessions page reads `listSessionsPaginated`.
 */
export const listSessions = query({
  args: {
    ...listFilters,
    limit: v.optional(v.number()),
  },
  returns: v.array(listedSessionValidator),
  handler: async (ctx, { limit, ...filters }) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const sessions = await (
      await readableSessions(ctx, caller, filters)
    ).take(limit ?? 50);
    return await listedRows(ctx, caller, sessions);
  },
});

/**
 * The sessions the caller may read, narrowed by the filters, page by page
 * (`usePaginatedQuery`): the Sessions page, one status per tab, and the
 * Reports page (status `completed`).
 */
export const listSessionsPaginated = query({
  args: {
    ...listFilters,
    paginationOpts: paginationOptsValidator,
  },
  returns: paginationResultValidator(listedSessionValidator),
  handler: async (ctx, { paginationOpts, ...filters }) => {
    const caller = await getCurrentUserOrThrow(ctx);
    const result = await (
      await readableSessions(ctx, caller, filters)
    ).paginate(paginationOpts);
    return { ...result, page: await listedRows(ctx, caller, result.page) };
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
 * The 100 most recent completed sessions the caller may read. The Reports
 * page of the previous version of the site reads it; this version reads
 * `listSessionsPaginated` with the status `completed`.
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
    const caller = await getCurrentUserOrThrow(ctx);
    const sessions = await (
      await readableSessions(ctx, caller, { status: "completed" })
    ).take(100);

    const result = [];
    for (const session of await stillReadable(ctx, caller, sessions)) {
      result.push({
        _id: session._id,
        status: session.status,
        startedAt: session.startedAt,
        endedAt: session.endedAt,
        channels: session.channels,
        notes: session.notes,
        ...(await namesOf(ctx, session)),
      });
    }
    return result;
  },
});
