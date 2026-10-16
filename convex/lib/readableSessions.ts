/**
 * The sessions a caller may read (ANH-158, ANH-227). Paginated lists and rider
 * history read one index range with `canAccessSession` as a filter. Short
 * lists merge bounded machine and rider ranges for an unfiltered gestionnaire
 * instead of scanning the centre's history. Other callers and explicit
 * machine/rider filters keep the single range. `sessions.ts` caps page scans;
 * `sessionReports.ts` reads the history of one rider (ANH-89).
 */
import type { Expression } from "convex/server";
import type { QueryCtx } from "../_generated/server";
import type { Doc, Id } from "../_generated/dataModel";
import { canAccessSession, sameOrganization, type CurrentUser } from "./auth";

/**
 * At this cap Convex returns a partial page (SplitRequired) with a cursor at
 * the stopping point. usePaginatedQuery splits it; later sessions remain
 * accessible through "Show more", using Convex's contiguous pagination.
 */
export const SESSION_PAGE_MAX_ROWS_READ = 1000;

export function pageMaxRowsRead(asked?: number): number {
  return asked !== undefined && Number.isFinite(asked) && asked >= 1
    ? Math.min(Math.floor(asked), SESSION_PAGE_MAX_ROWS_READ)
    : SESSION_PAGE_MAX_ROWS_READ;
}

/** What a list can be narrowed to, inside what the caller may read. */
export type ListFilters = {
  status?: Doc<"sessions">["status"];
  machineId?: Id<"machines">;
  userId?: Id<"users">;
};

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
export async function readableSessions(
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
 * The newest readable sessions, with at most (managed machines + 1) * limit
 * session reads for a gestionnaire without machine/rider filters, plus
 * machine_gestionnaires links and machines. Organisation and status are in
 * the index range, so no rejected rows are scanned. Each source takes at most
 * limit; their union contains the global newest limit, including creation ties.
 */
export async function newestReadableSessions(
  ctx: QueryCtx,
  caller: CurrentUser,
  filters: ListFilters,
  limit: number,
): Promise<Doc<"sessions">[]> {
  if (
    caller.role !== "gestionnaire" ||
    filters.machineId !== undefined ||
    filters.userId !== undefined
  ) {
    return await (await readableSessions(ctx, caller, filters)).take(limit);
  }

  const machines = await machinesManagedBy(ctx, caller);
  const { status } = filters;
  const sessions = ctx.db.query("sessions");
  const ranges = machines.map((machineId) =>
    status === undefined
      ? sessions.withIndex("by_organization_and_machine_and_started", (q) =>
          q
            .eq("organizationId", caller.organizationId)
            .eq("machineId", machineId),
        )
      : sessions.withIndex(
          "by_organization_and_machine_and_status_and_started",
          (q) =>
            q
              .eq("organizationId", caller.organizationId)
              .eq("machineId", machineId)
              .eq("status", status),
        ),
  );
  ranges.push(
    status === undefined
      ? sessions.withIndex("by_organization_and_user_and_started", (q) =>
          q
            .eq("organizationId", caller.organizationId)
            .eq("userId", caller._id),
        )
      : sessions.withIndex(
          "by_organization_and_user_and_status_and_started",
          (q) =>
            q
              .eq("organizationId", caller.organizationId)
              .eq("userId", caller._id)
              .eq("status", status),
        ),
  );
  const sources = await Promise.all(
    ranges.map((range) => range.order("desc").take(limit)),
  );
  const unique = new Map(
    sources.flat().map((session) => [session._id, session]),
  );
  return [...unique.values()]
    .sort(
      (a, b) => b.startedAt - a.startedAt || b._creationTime - a._creationTime,
    )
    .slice(0, limit);
}

/**
 * The sessions of a list that `canAccessSession`, the rule of every other read
 * of a session, still lets the caller read. The filter of `readableSessions`
 * is that rule, so this removes nothing while the two agree; if they ever
 * part, a session is left out rather than shown.
 */
export async function stillReadable(
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
