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
 * **One physical event, one incident.** The Pi records the verdict it
 * latches, then ends the session for it: an emergency stop is the verdict
 * `operator_estop` (or `drive_precommanded`, `enable_unconfirmed`) and the end
 * `emergency_stop`; a tick exception the verdict `tick_exception` and the end
 * of that name; a drive fault the event `drive_fault`, the verdict
 * `drive_fault` (`RAMP_DOWN`) and the end `safety_verdict`
 * (raspberry-pi/src/training/runtime.py, `_begin_ending`). Each of them is
 * one incident, of the kind of its verdict (`kindOfVerdict`), whichever of
 * its events and its end arrives first: an end that finds the verdict it was
 * given joins its incident; an end that finds none opens one named after
 * itself (`rule` = `kind`, a placeholder), at the level the end implies, which
 * the verdict or the fault joins when it arrives. A join keeps the higher
 * severity. And one incident per `(sessionId, rule, t)`: an event sent again
 * opens nothing.
 *
 * While a `high` incident of a machine is `open` or `analyzed`
 * (`openHighIncidents`), no programmed session is launched there
 * (`training.launchAutoSession`, `incident_open`) nor armed
 * (`armingRefusal`), and the machine's console is told the count on its poll
 * (`incidents_open_high`). A bench session, nobody on board, stays possible.
 */
import machineApi from "../../contracts/machine-api.json";
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import { incidentSummary, writeLogbook } from "./logbook";
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

const SEVERITIES: IncidentSeverity[] = ["low", "medium", "high"];

function higher(a: IncidentSeverity, b: IncidentSeverity): IncidentSeverity {
  return SEVERITIES.indexOf(a) >= SEVERITIES.indexOf(b) ? a : b;
}

/** How the Pi ends a session, for the ends that open an incident. */
type End = "safety_verdict" | "emergency_stop" | "tick_exception";
type StopKind = End | "drive_fault";

/** The Pi's verdicts that end a session on `emergency_stop` (runtime.py, safety.py). */
const ESTOP_RULES = ["operator_estop", "drive_precommanded", "enable_unconfirmed"];

/** The kind of the incident a stopping verdict of `rule` belongs to. */
export function kindOfVerdict(rule: string): StopKind {
  if (ESTOP_RULES.includes(rule)) return "emergency_stop";
  if (rule === "tick_exception") return "tick_exception";
  if (rule === "drive_fault") return "drive_fault";
  return "safety_verdict";
}

/** The end each kind of stop gives the session. */
const END_OF: Record<StopKind, End> = {
  safety_verdict: "safety_verdict",
  drive_fault: "safety_verdict",
  emergency_stop: "emergency_stop",
  tick_exception: "tick_exception",
};

/**
 * The level an end implies when none of its verdicts was received: an
 * emergency stop zeroes the reference (`QUICK_STOP`), a tick exception goes
 * silent, and a safety verdict may have been any stop up to `GO_SILENT`.
 */
export const END_LEVEL: Record<End, string> = {
  emergency_stop: "QUICK_STOP",
  tick_exception: "GO_SILENT",
  safety_verdict: UNKNOWN_LEVEL,
};

/**
 * The ends that open an incident, as the shared contract pins them
 * (`incidents.stop_ends`), the console's `STOP_ENDS` too.
 */
export const STOP_ENDS: readonly string[] = machineApi.incidents.stop_ends;

function isEnd(word: string): word is End {
  return STOP_ENDS.includes(word) && word in END_LEVEL;
}

/** What was seen of an incident: an event of the session, or its end. */
type Observation = {
  kind: IncidentKind;
  rule: string;
  t: number;
  level: string | null;
  /** From an event of the session: its rule and its instant are the precise ones. */
  fromEvent: boolean;
  recordId?: Id<"session_record_files">;
};

/** An incident named after its kind: its precise rule and instant are not known yet. */
function placeholder(incident: Doc<"incidents">): boolean {
  return incident.rule === incident.kind;
}

/**
 * A placeholder whose severity is the end's guess: one the end opened alone.
 * The verdict on a fault also names its incident after its kind
 * (`drive_fault`), but at the level it observed; a tick exception is `high`
 * either way.
 */
function guessed(incident: Doc<"incidents">): boolean {
  return placeholder(incident) && incident.kind !== "drive_fault";
}

/** The incidents a session has opened, a handful at most, oldest first. */
async function incidentsOf(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<Doc<"incidents">[]> {
  const rows = await ctx.db
    .query("incidents")
    .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
    .take(100);
  return rows.sort((a, b) => a._creationTime - b._creationTime);
}

/**
 * The incident that an observation of `kind` belongs to: a placeholder of the
 * same stop (an end that arrived first, or a fault's verdict before the
 * fault), closed or not, or, for the verdict on a fault (`faultVerdict`), the
 * fault itself if it is not closed. The latest of them.
 */
function joined(
  incidents: Doc<"incidents">[],
  kind: IncidentKind,
  faultVerdict: boolean,
): Doc<"incidents"> | undefined {
  const joins = (incident: Doc<"incidents">) => {
    if (faultVerdict && incident.kind === "drive_fault" && incident.status !== "closed")
      return true;
    if (!placeholder(incident)) return false;
    if (kind === "drive_fault")
      return incident.kind === "drive_fault" || incident.kind === "safety_verdict";
    return incident.kind === kind;
  };
  const found = incidents.filter(joins);
  return found[found.length - 1];
}

/**
 * Join `observation` to `incident`: for a placeholder, the precise rule, kind
 * and instant of an event, and the severity it observed in place of the end's
 * guess; otherwise the higher severity, an observed level never lowered. A
 * closed incident keeps what is joined to it, and opens again only when it
 * becomes `high`. Alerts the Anheart admins when it becomes `high`.
 */
async function join(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  incident: Doc<"incidents">,
  observation: Observation,
): Promise<Id<"incidents">> {
  const observed = severityOf(observation.kind, incident.occupied, observation.level);
  const precise = placeholder(incident) && observation.fromEvent;
  const severity =
    precise && guessed(incident) ? observed : higher(incident.severity, observed);
  const reopened =
    incident.status === "closed" && severity === "high" && incident.severity !== "high";
  await ctx.db.patch(incident._id, {
    severity,
    ...(precise
      ? { kind: observation.kind, rule: observation.rule, t: observation.t }
      : {}),
    ...(reopened
      ? { status: "open" as const, closedBy: undefined, closedAt: undefined }
      : {}),
  });
  if (severity === "high" && incident.severity !== "high") {
    await scheduleAlert(ctx, session.machineId, "incident_high", {
      sessionId: session._id,
    });
  }
  if (reopened) {
    const named = precise ? observation : incident;
    await writeLogbook(
      ctx,
      { _id: session.machineId, organizationId: session.organizationId },
      {
        kind: "incident",
        summary: incidentSummary(named, "rouvert", severity),
        refs: { incidentId: incident._id },
      },
    );
  }
  return incident._id;
}

/**
 * Open the incident of `session` for `observation`, or join the one already
 * open for the same `(rule, t)`. Schedules the alert to the Anheart admins
 * for a `high` one. Returns the incident.
 */
export async function openIncident(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  observation: Observation,
): Promise<Id<"incidents">> {
  const known = await ctx.db
    .query("incidents")
    .withIndex("by_session", (q) =>
      q
        .eq("sessionId", session._id)
        .eq("rule", observation.rule)
        .eq("t", observation.t),
    )
    .first();
  if (known) return await join(ctx, session, known, observation);
  const occupied = personOnBoard(session);
  const severity = severityOf(observation.kind, occupied, observation.level);
  const incidentId = await ctx.db.insert("incidents", {
    organizationId: session.organizationId,
    machineId: session.machineId,
    sessionId: session._id,
    kind: observation.kind,
    rule: observation.rule,
    t: observation.t,
    severity,
    occupied,
    status: "open",
    openedAt: Date.now(),
    recordId: observation.recordId,
  });
  if (severity === "high") {
    await scheduleAlert(ctx, session.machineId, "incident_high", {
      sessionId: session._id,
    });
  }
  await writeLogbook(
    ctx,
    { _id: session.machineId, organizationId: session.organizationId },
    {
      kind: "incident",
      summary: incidentSummary(observation, "ouvert", severity),
      refs: { incidentId },
    },
  );
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

/**
 * EX-2: the incident a newly stored event of `session` opens or joins. A
 * stopping verdict (`RAMP_DOWN` up) or a drive fault joins the incident of
 * its stop already open (`joined`); otherwise a verdict from `QUICK_STOP` up,
 * or a drive fault, opens one.
 */
export async function openIncidentOfEvent(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  event: Event,
): Promise<Id<"incidents"> | null> {
  let observation: Observation;
  if (event.kind === "verdict") {
    const verdict = verdictOf(event.detail);
    if (verdict === null || levelRank(verdict.level) < levelRank("RAMP_DOWN"))
      return null;
    observation = {
      kind: kindOfVerdict(verdict.rule),
      rule: verdict.rule,
      t: event.t,
      level: verdict.level,
      fromEvent: true,
    };
  } else if (event.kind === "drive_fault" && driveFaultAlerts(event.detail)) {
    // The supervisor's verdict on a drive fault is `RAMP_DOWN`.
    observation = {
      kind: "drive_fault",
      rule: driveFaultCode(event.detail) ?? "drive_fault",
      t: event.t,
      level: "RAMP_DOWN",
      fromEvent: true,
    };
  } else {
    return null;
  }
  const faultVerdict = event.kind === "verdict" && observation.kind === "drive_fault";
  const target = joined(
    await incidentsOf(ctx, session._id),
    observation.kind,
    faultVerdict,
  );
  if (target) return await join(ctx, session, target, observation);
  const opens =
    event.kind === "drive_fault" ||
    levelRank(observation.level) >= levelRank("QUICK_STOP");
  return opens ? await openIncident(ctx, session, observation) : null;
}

/** How many of a session's last events an end reads for its verdict. */
export const VERDICT_SCAN = 500;

/**
 * The verdict the Pi ended the session on with `end`: the earliest stopping
 * verdict (`RAMP_DOWN` up) among the session's last events that gives this
 * end. Null when none was received.
 */
async function endingVerdict(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
  end: End,
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
      END_OF[kindOfVerdict(verdict.rule)] === end
    )
      found = { ...verdict, t: event.t };
  }
  return found;
}

/**
 * The words the console ends a launch its pre-flight refused with, as the
 * shared contract pins them (`incidents.preflight_refusal_prefix`).
 */
export const PREFLIGHT_REFUSAL: string =
  machineApi.incidents.preflight_refusal_prefix;

/** How many pre-flight refusals in a row open an incident. */
export const PREFLIGHT_REFUSALS = 3;

function refusedByPreflight(session: Doc<"sessions">): boolean {
  return (
    session.status === "failed" &&
    session.endReason?.startsWith(PREFLIGHT_REFUSAL) === true
  );
}

/**
 * EX-2: the incident the end of `session` opens or joins. `reason` is what
 * the machine sent, `t` the end on the machine's clock. Called once the
 * session is ended.
 */
export async function openIncidentOfEnd(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  reason: string,
  t: number,
): Promise<Id<"incidents"> | null> {
  const end = reason.split(":")[0].trim();
  if (isEnd(end)) {
    const verdict = await endingVerdict(ctx, session._id, end);
    if (verdict === null) {
      // Its verdict not received: the stop already open that gives this end
      // (a fault whose verdict is late) is it; else the end alone, at the
      // level it implies.
      const open = (await incidentsOf(ctx, session._id)).filter(
        (row) =>
          row.status !== "closed" &&
          row.kind !== "preflight_repeated_failure" &&
          row.kind !== "record_rejected" &&
          row.kind !== "manual" &&
          END_OF[row.kind] === end,
      );
      const stop = open[open.length - 1];
      const alone: Observation = {
        kind: end,
        rule: end,
        t,
        level: END_LEVEL[end],
        fromEvent: false,
      };
      return stop
        ? await join(ctx, session, stop, { ...alone, kind: stop.kind, level: null })
        : await openIncident(ctx, session, alone);
    }
    const observation: Observation = {
      kind: kindOfVerdict(verdict.rule),
      ...verdict,
      fromEvent: true,
    };
    const incidents = await incidentsOf(ctx, session._id);
    const target =
      incidents.find((row) => row.rule === verdict.rule && row.t === verdict.t) ??
      joined(incidents, observation.kind, observation.kind === "drive_fault");
    return target
      ? await join(ctx, session, target, observation)
      : await openIncident(ctx, session, observation);
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
    fromEvent: false,
  });
}

/**
 * The end of a session on its machine's axis, where its events are dated:
 * the server's date shifted back by the machine's own start (`machineAxis`
 * in convex/training.ts), the start when it has not ended.
 */
export function machineEndOf(session: Doc<"sessions">): number {
  const at = session.endedAt ?? session.startedAt;
  return session.machineStartedAt === undefined
    ? at
    : at - (session.startedAt - session.machineStartedAt);
}

/** The most `high` incidents counted of one status (the poll's figure is bounded). */
export const HIGH_COUNT_LIMIT = 100;

/**
 * How many `high` incidents of the machine are `open` or `analyzed`,
 * `2 * HIGH_COUNT_LIMIT` at most: only those are read.
 */
export async function openHighIncidents(
  ctx: QueryCtx,
  machineId: Id<"machines">,
): Promise<number> {
  let count = 0;
  for (const status of ["open", "analyzed"] as const) {
    const rows = await ctx.db
      .query("incidents")
      .withIndex("by_machine_and_status_and_severity", (q) =>
        q.eq("machineId", machineId).eq("status", status).eq("severity", "high"),
      )
      .take(HIGH_COUNT_LIMIT);
    count += rows.length;
  }
  return count;
}
