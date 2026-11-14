/**
 * The machine logbook (ANH-149): one chronological line per machine.
 *
 * Written by the server where something happens to the machine, never by a
 * client for it:
 * - an incident opened or opened again (`openIncident`, convex/lib/incidents.ts)
 *   and closed (`incidents.close`): kind `incident`, its id in `refs`;
 * - a validation review added, or the level lowered (`machineRegistry`):
 *   `validation_review`, the report in `refs.storageId`;
 * - a drive parameter sheet attached (`machineRegistry.attachDriveParameterSheet`):
 *   `drive_parameter_change`, the sheet in `refs.storageId`;
 * - a software version the heartbeat says changed (`machines.recordHeartbeat`):
 *   `software_update`, the new version in `refs.softwareVersion`.
 * And by hand, by an admin (`machineLogbook.addEntry`): an intervention, a
 * calibration, a part replaced, a note; an org_admin writes notes only.
 *
 * **The drive fault rule.** After a `drive_fault` incident whose LFT code the
 * machine does not reset at the console (`manual_reset_only` in
 * contracts/drive-faults.json, the table the Pi pins its own on), no
 * programmed session is launched on the machine until that incident is
 * closed AND an `intervention` entry names it (`driveInterventionsDue`). A
 * code the table does not know counts as `manual_reset_only`. A bench session
 * stays possible: it is how the machine is tested after the intervention.
 *
 * Entries are never edited nor deleted. Identifiers only: who wrote one is a
 * user id, and the server's own summaries name rules, codes, levels and
 * versions, never a person.
 */
import type { Infer } from "convex/values";
import driveFaults from "../../contracts/drive-faults.json";
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import type { logbookKindValidator } from "../schema";

export type LogbookKind = Infer<typeof logbookKindValidator>;
export type LogbookRefs = Doc<"machine_logbook">["refs"];

/** Characters of an entry's one line, at most. */
export const LOGBOOK_SUMMARY_MAX = 200;
/** Characters of an entry's details, at most. */
export const LOGBOOK_DETAILS_MAX = 2000;

/** What an admin writes by hand; everything else the server writes. */
export const MANUAL_KINDS: readonly LogbookKind[] = [
  "intervention",
  "calibration",
  "part_replaced",
  "note",
];
/** What an org_admin may write on a machine of their organisation. */
export const ORGANIZATION_KINDS: readonly LogbookKind[] = ["note"];

const POLICIES: Readonly<Record<string, string>> = driveFaults.faults;

/**
 * Whether a drive fault of this LFT mnemonic waits for an intervention: the
 * shared table's `manual_reset_only`, or a code it does not know.
 */
export function manualResetOnly(mnemonic: string): boolean {
  return POLICIES[mnemonic] !== "console_reset";
}

/** Write one entry on the machine's line of time, now unless `at` says when. */
export async function writeLogbook(
  ctx: MutationCtx,
  machine: { _id: Id<"machines">; organizationId?: Id<"organizations"> },
  entry: {
    kind: LogbookKind;
    summary: string;
    details?: string;
    by?: Id<"users">;
    refs?: LogbookRefs;
    at?: number;
  },
): Promise<Id<"machine_logbook">> {
  return await ctx.db.insert("machine_logbook", {
    machineId: machine._id,
    organizationId: machine.organizationId,
    at: entry.at ?? Date.now(),
    kind: entry.kind,
    summary: entry.summary.slice(0, LOGBOOK_SUMMARY_MAX),
    details: entry.details,
    by: entry.by,
    refs: entry.refs ?? {},
  });
}

/** The one line the server writes for an incident: its kind, its rule, what became of it. */
export function incidentSummary(
  incident: Pick<Doc<"incidents">, "kind" | "rule">,
  what: "ouvert" | "rouvert" | "clos",
  severity?: Doc<"incidents">["severity"],
): string {
  const level = severity === undefined ? "" : ` (${severity})`;
  return `incident ${incident.kind} ${incident.rule} ${what}${level}`;
}

/**
 * Whether an `intervention` entry names the incident and was written (the
 * server's creation time, never the date the admin gave it) after the
 * incident last opened: its opening, or its last reopening.
 */
async function intervened(
  ctx: QueryCtx,
  incident: Doc<"incidents">,
): Promise<boolean> {
  const since = incident.reopenedAt ?? incident.openedAt;
  const last = await ctx.db
    .query("machine_logbook")
    .withIndex("by_incident_and_kind", (q) =>
      q.eq("refs.incidentId", incident._id).eq("kind", "intervention"),
    )
    .order("desc")
    .first();
  return last !== null && last._creationTime > since;
}

/**
 * Keep the incident's `interventionDue` true while it is a drive fault the
 * console cannot reset and is not resolved: not closed, or closed with no
 * intervention written since it last opened. Called wherever one of these
 * changes: its opening and every join, its closing, an intervention naming it.
 */
export async function refreshInterventionDue(
  ctx: MutationCtx,
  incidentId: Id<"incidents">,
): Promise<void> {
  const incident = await ctx.db.get(incidentId);
  if (incident === null) return;
  const due =
    incident.kind === "drive_fault" &&
    manualResetOnly(incident.rule) &&
    !(incident.status === "closed" && (await intervened(ctx, incident)));
  if ((incident.interventionDue ?? false) !== due)
    await ctx.db.patch(incident._id, { interventionDue: due ? true : undefined });
}

/**
 * The drive fault incidents of the machine that still close its programmed
 * sessions (`interventionDue`). Read by an index on those alone: a machine's
 * resolved faults, however many, are never read.
 */
export async function driveInterventionsDue(
  ctx: QueryCtx,
  machineId: Id<"machines">,
): Promise<Id<"incidents">[]> {
  const due = await ctx.db
    .query("incidents")
    .withIndex("by_machine_and_intervention_due", (q) =>
      q.eq("machineId", machineId).eq("interventionDue", true),
    )
    .collect();
  return due.map((incident) => incident._id);
}

/**
 * A software version the heartbeat says changed, written once: not when the
 * last entry already says it, nor when the console only went back to the
 * version it left (a console flapping between two versions writes one entry,
 * and pushes no real one out of the list).
 */
export async function writeSoftwareUpdate(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  previous: string,
  next: string,
  at: number,
): Promise<void> {
  const [last, before] = await ctx.db
    .query("machine_logbook")
    .withIndex("by_machine_and_kind_and_at", (q) =>
      q.eq("machineId", machine._id).eq("kind", "software_update"),
    )
    .order("desc")
    .take(2);
  const said = last?.refs.softwareVersion;
  const flapping = said === previous && before?.refs.softwareVersion === next;
  if (said === next || flapping) return;
  await writeLogbook(ctx, machine, {
    kind: "software_update",
    summary: `logiciel ${previous} -> ${next}`,
    at,
    refs: { softwareVersion: next },
  });
}
