/**
 * The incident register (ANH-145): what opens an incident, how severe it is,
 * and what an open one closes.
 *
 * Opened by the server where it hears of the event, never by a client:
 * - `training.storeEvents`, for a `verdict` event whose action is
 *   `QUICK_STOP` or `GO_SILENT` (or an action this server does not know),
 *   even when the session goes on, and for a `drive_fault` event;
 * - `training.endTrainingSession`, for an end `emergency_stop`,
 *   `safety_verdict` or `tick_exception`, and for the third pre-flight
 *   refusal in a row of a launch on the same machine;
 * - `recordStorage`, when a session's record becomes `rejected`.
 *
 * One incident per `(sessionId, rule, t)`: an event sent again opens nothing.
 * An end `safety_verdict` takes the rule and the instant of the most severe
 * stopping verdict among the session's last events, so the verdict that
 * already opened one is not counted twice.
 *
 * While a `high` incident of a machine is `open` or `analyzed`
 * (`openHighIncidents`), no programmed session is launched there
 * (`training.launchAutoSession`, `incident_open`) nor armed
 * (`armingRefusal`), and the machine's console is told the count on its poll
 * (`incidents_open_high`). A bench session, nobody on board, stays possible.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import { personOnBoard } from "./machineRegistry";
import {
  driveFaultAlerts,
  driveFaultCode,
  scheduleAlert,
  verdictAction,
} from "./notifications";

export type IncidentKind = Doc<"incidents">["kind"];
export type IncidentSeverity = Doc<"incidents">["severity"];

/**
 * The Pi's safety actions, least severe first (`SafetyAction`,
 * raspberry-pi/src/training/types.py).
 */
const LEVELS = ["FREEZE", "REDUCE", "RAMP_DOWN", "QUICK_STOP", "GO_SILENT"];

/** The level of a verdict whose action this server cannot read. */
export const UNKNOWN_LEVEL = "UNKNOWN";

/**
 * The rank of a level: -1 for none (`null`), above `GO_SILENT` for an action
 * this server does not know, which is the safe side.
 */
export function levelRank(level: string | null): number {
  if (level === null) return -1;
  const rank = LEVELS.indexOf(level);
  return rank === -1 ? LEVELS.length : rank;
}

/**
 * EX-3: `high` with a person on board from `RAMP_DOWN` up, or at
 * `GO_SILENT` (or an unknown action), or for a tick exception; `medium` for a
 * drive fault or a verdict at the bench; `low` otherwise.
 */
export function severityOf(
  kind: IncidentKind,
  occupied: boolean,
  level: string | null,
): IncidentSeverity {
  if (kind === "tick_exception") return "high";
  if (levelRank(level) >= levelRank("GO_SILENT")) return "high";
  if (occupied && levelRank(level) >= levelRank("RAMP_DOWN")) return "high";
  if (kind === "drive_fault" || kind === "safety_verdict") return "medium";
  return "low";
}

type Opening = {
  kind: IncidentKind;
  rule: string;
  t: number;
  level: string | null;
  recordId?: Id<"session_record_files">;
};

/**
 * Open the incident of `session`, unless one is already open for the same
 * `(rule, t)`. Schedules the alert to the Anheart admins for a `high` one.
 * Returns the new incident, or null.
 */
export async function openIncident(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  opening: Opening,
): Promise<Id<"incidents"> | null> {
  const known = await ctx.db
    .query("incidents")
    .withIndex("by_session", (q) =>
      q
        .eq("sessionId", session._id)
        .eq("rule", opening.rule)
        .eq("t", opening.t),
    )
    .first();
  if (known) return null;
  const occupied = personOnBoard(session);
  const severity = severityOf(opening.kind, occupied, opening.level);
  const incidentId = await ctx.db.insert("incidents", {
    organizationId: session.organizationId,
    machineId: session.machineId,
    sessionId: session._id,
    kind: opening.kind,
    rule: opening.rule,
    t: opening.t,
    severity,
    occupied,
    status: "open",
    openedAt: Date.now(),
    recordId: opening.recordId,
  });
  if (severity === "high") {
    await scheduleAlert(ctx, session.machineId, "incident_high", {
      sessionId: session._id,
    });
  }
  return incidentId;
}

/** A verdict's rule and level as the Pi writes it: "<rule> (<ACTION>): <detail>". */
const VERDICT = /^([a-z0-9_]{1,64}) \(/;

/** The rule and the level of a `verdict` event; null when it lifts the verdict. */
function verdictOf(detail: string): { rule: string; level: string } | null {
  if (detail.trim() === "cleared") return null;
  return {
    rule: VERDICT.exec(detail)?.[1] ?? "unknown",
    level: verdictAction(detail) ?? UNKNOWN_LEVEL,
  };
}

type Event = { t: number; kind: string; detail: string };

/** EX-2: the incident a newly stored event of `session` opens, if any. */
export async function openIncidentOfEvent(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  event: Event,
): Promise<Id<"incidents"> | null> {
  if (event.kind === "verdict") {
    const verdict = verdictOf(event.detail);
    if (verdict === null || levelRank(verdict.level) < levelRank("QUICK_STOP"))
      return null;
    return await openIncident(ctx, session, {
      kind: "safety_verdict",
      rule: verdict.rule,
      t: event.t,
      level: verdict.level,
    });
  }
  if (event.kind === "drive_fault" && driveFaultAlerts(event.detail)) {
    // The supervisor's verdict on a drive fault is `RAMP_DOWN`.
    return await openIncident(ctx, session, {
      kind: "drive_fault",
      rule: driveFaultCode(event.detail) ?? "drive_fault",
      t: event.t,
      level: "RAMP_DOWN",
    });
  }
  return null;
}

/** How many of a session's last events an end `safety_verdict` reads. */
export const VERDICT_SCAN = 500;

/**
 * The most severe stopping verdict among the session's last events, the
 * latest of equals; null when none was received.
 */
async function stoppingVerdict(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<{ rule: string; level: string; t: number } | null> {
  const events = await ctx.db
    .query("training_events")
    .withIndex("by_session_and_seq", (q) => q.eq("sessionId", sessionId))
    .order("desc")
    .take(VERDICT_SCAN);
  let found: { rule: string; level: string; t: number } | null = null;
  for (const event of events) {
    const verdict = event.kind === "verdict" ? verdictOf(event.detail) : null;
    if (
      verdict !== null &&
      levelRank(verdict.level) >= levelRank("RAMP_DOWN") &&
      (found === null || levelRank(verdict.level) > levelRank(found.level))
    )
      found = { ...verdict, t: event.t };
  }
  return found;
}

/** The words the console ends a launch its pre-flight refused with. */
export const PREFLIGHT_REFUSAL = "refusee par la machine : pre-vol :";

/** How many pre-flight refusals in a row open an incident. */
export const PREFLIGHT_REFUSALS = 3;

function refusedByPreflight(session: Doc<"sessions">): boolean {
  return (
    session.status === "failed" &&
    session.endReason?.startsWith(PREFLIGHT_REFUSAL) === true
  );
}

/**
 * EX-2: the incident the end of `session` opens, if any. `reason` is what the
 * machine sent, `t` the end on the machine's clock. Called once the session
 * is ended.
 */
export async function openIncidentOfEnd(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  reason: string,
  t: number,
): Promise<Id<"incidents"> | null> {
  const end = reason.split(":")[0].trim();
  if (end === "emergency_stop" || end === "tick_exception") {
    return await openIncident(ctx, session, {
      kind: end,
      rule: end,
      t,
      // The screen's E-STOP zeroes the reference at once.
      level: end === "emergency_stop" ? "QUICK_STOP" : null,
    });
  }
  if (end === "safety_verdict") {
    const verdict = await stoppingVerdict(ctx, session._id);
    return await openIncident(ctx, session, {
      kind: "safety_verdict",
      ...(verdict ?? { rule: "safety_verdict", level: "RAMP_DOWN", t }),
    });
  }
  if (!refusedByPreflight(session)) return null;
  // Exactly the third refusal in a row on this machine, most recent first.
  const latest = await ctx.db
    .query("sessions")
    .withIndex("by_machine_and_started", (q) =>
      q.eq("machineId", session.machineId),
    )
    .order("desc")
    .take(PREFLIGHT_REFUSALS + 1);
  const run = latest.findIndex((row) => !refusedByPreflight(row));
  if ((run === -1 ? latest.length : run) !== PREFLIGHT_REFUSALS) return null;
  return await openIncident(ctx, session, {
    kind: "preflight_repeated_failure",
    rule: "preflight",
    t,
    level: null,
  });
}

/** How many `high` incidents of the machine are `open` or `analyzed`. */
export async function openHighIncidents(
  ctx: QueryCtx,
  machineId: Id<"machines">,
): Promise<number> {
  let count = 0;
  for (const status of ["open", "analyzed"] as const) {
    const rows = await ctx.db
      .query("incidents")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", machineId).eq("status", status),
      )
      .collect();
    count += rows.filter((row) => row.severity === "high").length;
  }
  return count;
}
