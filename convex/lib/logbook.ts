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
 * The `drive_fault` incidents of the machine whose code is not reset at the
 * console and that still close its programmed sessions: not closed yet, or
 * closed with no `intervention` entry that names them.
 */
export async function driveInterventionsDue(
  ctx: QueryCtx,
  machineId: Id<"machines">,
): Promise<Id<"incidents">[]> {
  const faults = await ctx.db
    .query("incidents")
    .withIndex("by_machine_and_kind", (q) =>
      q.eq("machineId", machineId).eq("kind", "drive_fault"),
    )
    .collect();
  const due: Id<"incidents">[] = [];
  for (const incident of faults) {
    if (!manualResetOnly(incident.rule)) continue;
    const intervention =
      incident.status === "closed"
        ? await ctx.db
            .query("machine_logbook")
            .withIndex("by_incident_and_kind", (q) =>
              q.eq("refs.incidentId", incident._id).eq("kind", "intervention"),
            )
            .first()
        : null;
    if (intervention === null) due.push(incident._id);
  }
  return due;
}
