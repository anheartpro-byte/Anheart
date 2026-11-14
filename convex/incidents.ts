/**
 * The incident register as the site reads it (ANH-145): the list, an
 * incident's sheet, its analysis and its closing.
 *
 * The incidents themselves are opened by the server where it hears of the
 * event (convex/lib/incidents.ts). Here the staff read those of the machines
 * they reach (`canAccessMachine`): the Anheart admin every organisation's, an
 * organisation admin their organisation's, a gestionnaire those of the
 * machines they manage. A patient reads none. They analyse them; only the
 * Anheart admin closes one, with its cause and its action, which is what
 * opens the machine to programmed sessions again.
 */
import { v } from "convex/values";
import {
  internalQuery,
  mutation,
  query,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import {
  canAccessMachine,
  canAccessSession,
  inScope,
  requireRole,
  STAFF_ROLES,
  type CurrentUser,
} from "./lib/auth";
import { audit, userActor } from "./lib/audit";
import { refusal } from "./lib/errors";
import { openHighIncidents } from "./lib/incidents";
import { incidentSummary, writeLogbook } from "./lib/logbook";
import { limitUser } from "./lib/rateLimits";
import {
  incidentKindValidator,
  incidentSeverityValidator,
  incidentStatusValidator,
} from "./schema";

/** The most recent incidents a list holds. */
// shortcut: no pagination; the fleet opens a few a month. Paginate past it.
export const INCIDENT_LIST_LIMIT = 1000;

/** The longest cause or action, in characters. */
export const INCIDENT_TEXT_MAX = 2000;

/** The events an incident's sheet shows around its instant, in ms each side. */
export const INCIDENT_EVENT_WINDOW_MS = 60_000;

/** The most events a sheet shows. */
export const INCIDENT_EVENT_LIMIT = 200;

const incidentRow = v.object({
  _id: v.id("incidents"),
  _creationTime: v.number(),
  organizationId: v.optional(v.id("organizations")),
  machineId: v.id("machines"),
  machineName: v.string(),
  sessionId: v.optional(v.id("sessions")),
  kind: incidentKindValidator,
  rule: v.string(),
  t: v.number(),
  severity: incidentSeverityValidator,
  occupied: v.boolean(),
  status: incidentStatusValidator,
  openedAt: v.number(),
  analysis: v.optional(v.string()),
  action: v.optional(v.string()),
  analyzedBy: v.optional(v.id("users")),
  closedBy: v.optional(v.id("users")),
  closedAt: v.optional(v.number()),
  recordId: v.optional(v.id("session_record_files")),
});

async function rowsOf(ctx: QueryCtx, incidents: Doc<"incidents">[]) {
  const names = new Map<Id<"machines">, string>();
  const rows = [];
  for (const incident of incidents) {
    let name = names.get(incident.machineId);
    if (name === undefined) {
      name = (await ctx.db.get(incident.machineId))?.name ?? "-";
      names.set(incident.machineId, name);
    }
    rows.push({ ...incident, machineName: name });
  }
  return rows;
}

/** The incidents the caller reads, most recently opened first. */
async function visibleIncidents(
  ctx: QueryCtx,
  caller: CurrentUser,
): Promise<Doc<"incidents">[]> {
  if (caller.role === "admin")
    return await ctx.db
      .query("incidents")
      .order("desc")
      .take(INCIDENT_LIST_LIMIT);
  if (caller.role === "org_admin")
    return await ctx.db
      .query("incidents")
      .withIndex("by_organization", (q) =>
        q.eq("organizationId", caller.organizationId),
      )
      .order("desc")
      .take(INCIDENT_LIST_LIMIT);
  const links = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", caller._id))
    .collect();
  const rows: Doc<"incidents">[] = [];
  for (const link of links) {
    if (link.organizationId !== caller.organizationId) continue;
    // The most recent of each machine, whatever their status.
    const latest = await ctx.db
      .query("incidents")
      .withIndex("by_machine", (q) => q.eq("machineId", link.machineId))
      .order("desc")
      .take(INCIDENT_LIST_LIMIT);
    rows.push(...latest.filter((row) => inScope(caller, row.organizationId)));
  }
  return rows
    .sort((a, b) => b._creationTime - a._creationTime)
    .slice(0, INCIDENT_LIST_LIMIT);
}

/**
 * The incidents of the machines the caller reaches, most recently opened
 * first, `INCIDENT_LIST_LIMIT` at most: what the page lists and what the
 * Anheart admin exports as CSV. Refused to a patient.
 */
export const list = query({
  args: {},
  returns: v.array(incidentRow),
  handler: async (ctx) => {
    const caller = await requireRole(ctx, STAFF_ROLES);
    return await rowsOf(ctx, await visibleIncidents(ctx, caller));
  },
});

/**
 * The incident, when it is in the caller's scope and the caller reaches its
 * machine; null otherwise. An incident is always its machine's organisation's:
 * both are checked, as `canAccessSession` checks a session's.
 */
async function reachable(
  ctx: QueryCtx,
  caller: CurrentUser,
  incidentId: Id<"incidents">,
): Promise<Doc<"incidents"> | null> {
  const incident = await ctx.db.get(incidentId);
  if (!incident || !inScope(caller, incident.organizationId)) return null;
  return (await canAccessMachine(ctx, incident.machineId, caller))
    ? incident
    : null;
}

function name(user: Doc<"users"> | null): string | null {
  return user === null ? null : `${user.firstName} ${user.lastName}`.trim();
}

/**
 * One incident's sheet: the incident, who analysed and closed it, its
 * session, and the session's events within `INCIDENT_EVENT_WINDOW_MS` of
 * its instant. Null when the caller does not reach its machine.
 */
export const get = query({
  args: { incidentId: v.id("incidents") },
  returns: v.union(
    v.object({
      incident: incidentRow,
      analyzedByName: v.union(v.string(), v.null()),
      closedByName: v.union(v.string(), v.null()),
      session: v.union(
        v.object({
          _id: v.id("sessions"),
          status: v.string(),
          startedAt: v.number(),
          endedAt: v.optional(v.number()),
          endReason: v.optional(v.string()),
          record: v.optional(v.string()),
        }),
        v.null(),
      ),
      events: v.array(
        v.object({
          t: v.number(),
          kind: v.string(),
          detail: v.string(),
        }),
      ),
      canClose: v.boolean(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const caller = await requireRole(ctx, STAFF_ROLES);
    const incident = await reachable(ctx, caller, args.incidentId);
    if (incident === null) return null;
    const session =
      incident.sessionId === undefined
        ? null
        : await ctx.db.get(incident.sessionId);
    const readable =
      session !== null && (await canAccessSession(ctx, session, caller));
    const events = readable
      ? await ctx.db
          .query("training_events")
          .withIndex("by_session_and_t", (q) =>
            q
              .eq("sessionId", session._id)
              .gte("t", incident.t - INCIDENT_EVENT_WINDOW_MS)
              .lte("t", incident.t + INCIDENT_EVENT_WINDOW_MS),
          )
          .take(INCIDENT_EVENT_LIMIT)
      : [];
    const [row] = await rowsOf(ctx, [incident]);
    return {
      incident: row,
      analyzedByName:
        incident.analyzedBy === undefined
          ? null
          : name(await ctx.db.get(incident.analyzedBy)),
      closedByName:
        incident.closedBy === undefined
          ? null
          : name(await ctx.db.get(incident.closedBy)),
      session: readable
        ? {
            _id: session._id,
            status: session.status,
            startedAt: session.startedAt,
            endedAt: session.endedAt,
            endReason: session.endReason,
            record: session.record,
          }
        : null,
      events: events.map(({ t, kind, detail }) => ({ t, kind, detail })),
      canClose: caller.role === "admin" && incident.status !== "closed",
    };
  },
});

/** A cause or an action as stored: trimmed, 1 to `INCIDENT_TEXT_MAX` characters. */
function text(value: string): string {
  const trimmed = value.trim();
  if (trimmed === "" || trimmed.length > INCIDENT_TEXT_MAX)
    throw refusal("incident_text_required");
  return trimmed;
}

const review = {
  incidentId: v.id("incidents"),
  analysis: v.string(),
  action: v.string(),
};

/**
 * Record the cause of an incident and the action taken (`analyzed`), by the
 * staff who reach its machine. An analysed `high` incident still closes
 * programmed sessions: only its closing reopens them.
 */
export const analyze = mutation({
  args: review,
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, STAFF_ROLES);
    const incident = await reachable(ctx, caller, args.incidentId);
    if (incident === null) throw refusal("incident_not_found");
    if (incident.status === "closed") throw refusal("incident_closed");
    await ctx.db.patch(incident._id, {
      status: "analyzed",
      analysis: text(args.analysis),
      action: text(args.action),
      analyzedBy: caller._id,
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: incident.organizationId,
      action: "incident.analyzed",
      targetId: incident._id,
      details: { machineId: incident.machineId },
    });
    return null;
  },
});

/**
 * Close an incident, with its cause and its action: the Anheart admin only.
 * The last `high` incident of a machine closed, programmed sessions may be
 * launched on it again. `analyzedBy` stays whoever wrote the cause and the
 * action as they are: the analyst when the admin closes on their text, the
 * admin when the admin writes it (or changes it).
 */
export const close = mutation({
  args: review,
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ["admin"]);
    const incident = await ctx.db.get(args.incidentId);
    if (!incident) throw refusal("incident_not_found");
    if (incident.status === "closed") throw refusal("incident_closed");
    const analysis = text(args.analysis);
    const action = text(args.action);
    const unchanged =
      analysis === incident.analysis && action === incident.action;
    await ctx.db.patch(incident._id, {
      status: "closed",
      analysis,
      action,
      analyzedBy:
        unchanged && incident.analyzedBy !== undefined
          ? incident.analyzedBy
          : caller._id,
      closedBy: caller._id,
      closedAt: Date.now(),
    });
    await writeLogbook(
      ctx,
      { _id: incident.machineId, organizationId: incident.organizationId },
      {
        kind: "incident",
        summary: incidentSummary(incident, "clos"),
        by: caller._id,
        refs: { incidentId: incident._id },
      },
    );
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: incident.organizationId,
      action: "incident.closed",
      targetId: incident._id,
      details: { machineId: incident.machineId },
    });
    return null;
  },
});

/** What a machine's poll announces: its `high` incidents open or analysed. */
export const openHighCount = internalQuery({
  args: { machineId: v.id("machines") },
  returns: v.number(),
  handler: async (ctx, args) => await openHighIncidents(ctx, args.machineId),
});
