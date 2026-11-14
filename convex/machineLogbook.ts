/**
 * The machine logbook, read and written by hand (ANH-149). What writes it,
 * and the drive fault rule, are in convex/lib/logbook.ts.
 *
 * Read by whoever reaches the machine (an Anheart admin, an org_admin of its
 * organisation, a gestionnaire who manages it). Written by hand by an Anheart
 * admin (an intervention, a calibration, a part replaced, a note) and by an
 * org_admin of the machine's organisation (a note only), with one optional
 * attachment in Convex Storage. Never edited, never deleted.
 */
import { v } from "convex/values";
import { mutation, query, type MutationCtx } from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import { audit, userActor } from "./lib/audit";
import {
  ORGANIZATION_ADMIN_ROLES,
  STAFF_ROLES,
  canAccessMachine,
  inScope,
  requireRole,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import {
  LOGBOOK_DETAILS_MAX,
  LOGBOOK_SUMMARY_MAX,
  MANUAL_KINDS,
  ORGANIZATION_KINDS,
  driveInterventionsDue,
  refreshInterventionDue,
  writeLogbook,
  type LogbookKind,
} from "./lib/logbook";
import { limitUser } from "./lib/rateLimits";
import { logbookKindValidator } from "./schema";

/** The most recent entries a machine's line of time shows, at most. */
export const LOGBOOK_LIST_LIMIT = 500;
/** What an attachment may be: a report, a photograph. */
const ATTACHMENT_TYPES: readonly string[] = [
  "application/pdf",
  "image/jpeg",
  "image/png",
];
const ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024;
/** An upload older than this is not attached: it was meant for something else. */
const ATTACHMENT_FRESH_MS = 3_600_000;

const refsValidator = v.object({
  incidentId: v.optional(v.id("incidents")),
  configRevision: v.optional(v.number()),
  softwareVersion: v.optional(v.string()),
  storageId: v.optional(v.id("_storage")),
});

const entryRow = v.object({
  _id: v.id("machine_logbook"),
  at: v.number(),
  kind: logbookKindValidator,
  summary: v.string(),
  details: v.optional(v.string()),
  by: v.optional(v.id("users")),
  byName: v.union(v.string(), v.null()),
  refs: refsValidator,
  attachmentUrl: v.union(v.string(), v.null()),
});

/** The kinds `caller` may write by hand on a machine of `organizationId`. */
function writable(
  caller: CurrentUser,
  organizationId: Id<"organizations"> | undefined,
): readonly LogbookKind[] {
  if (caller.role === "admin") return MANUAL_KINDS;
  if (caller.role === "org_admin" && sameOrganization(caller, organizationId))
    return ORGANIZATION_KINDS;
  return [];
}

/**
 * A machine's logbook, most recent first: its entries, the drive fault
 * incidents that still close its programmed sessions, and the kinds the
 * caller may add. Null when the caller does not reach the machine.
 */
export const list = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.null(),
    v.object({
      entries: v.array(entryRow),
      interventionsDue: v.array(v.id("incidents")),
      writable: v.array(logbookKindValidator),
    }),
  ),
  handler: async (ctx, args) => {
    const caller = await requireRole(ctx, STAFF_ROLES);
    const machine = await ctx.db.get(args.machineId);
    if (
      machine === null ||
      !inScope(caller, machine.organizationId) ||
      !(await canAccessMachine(ctx, machine._id, caller))
    )
      return null;
    const rows = await ctx.db
      .query("machine_logbook")
      .withIndex("by_machine_and_at", (q) => q.eq("machineId", machine._id))
      .order("desc")
      .take(LOGBOOK_LIST_LIMIT);
    const names = new Map<Id<"users">, string | null>();
    const entries = [];
    for (const row of rows) {
      let byName: string | null = null;
      if (row.by !== undefined) {
        if (!names.has(row.by)) {
          const user = await ctx.db.get(row.by);
          names.set(
            row.by,
            user === null ? null : `${user.firstName} ${user.lastName}`.trim(),
          );
        }
        byName = names.get(row.by) ?? null;
      }
      const storageId = row.refs.storageId;
      entries.push({
        _id: row._id,
        at: row.at,
        kind: row.kind,
        summary: row.summary,
        details: row.details,
        by: row.by,
        byName,
        refs: row.refs,
        attachmentUrl:
          storageId === undefined ? null : await ctx.storage.getUrl(storageId),
      });
    }
    return {
      entries,
      interventionsDue: await driveInterventionsDue(ctx, machine._id),
      writable: [...writable(caller, machine.organizationId)],
    };
  },
});

/** Issues an attachment upload URL to whoever may write an entry by hand. */
export const generateUploadUrl = mutation({
  args: {},
  returns: v.string(),
  handler: async (ctx) => {
    await limitUser(ctx);
    await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    return await ctx.storage.generateUploadUrl();
  },
});

/** A fresh upload of an accepted type and size, attached to no entry yet. */
async function attachment(
  ctx: MutationCtx,
  storageId: Id<"_storage">,
): Promise<Id<"_storage">> {
  const file = await ctx.db.system.get(storageId);
  if (
    file === null ||
    !ATTACHMENT_TYPES.includes(file.contentType ?? "") ||
    file.size > ATTACHMENT_MAX_BYTES ||
    Date.now() - file._creationTime >= ATTACHMENT_FRESH_MS
  )
    throw refusal("invalid_logbook_file");
  const used = await ctx.db
    .query("machine_logbook")
    .withIndex("by_storage", (q) => q.eq("refs.storageId", storageId))
    .first();
  if (used !== null) throw refusal("invalid_logbook_file");
  return storageId;
}

function line(value: string, max: number): string {
  const trimmed = value.trim();
  if (trimmed.length < 3 || trimmed.length > max)
    throw refusal("invalid_logbook_entry");
  return trimmed;
}

/** The incident an intervention names: one of the same machine. */
async function machineIncident(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  incidentId: Id<"incidents">,
): Promise<Id<"incidents">> {
  const incident = await ctx.db.get(incidentId);
  if (incident === null || incident.machineId !== machine._id)
    throw refusal("logbook_incident_mismatch");
  return incident._id;
}

/**
 * Write an entry by hand. An Anheart admin writes an intervention, a
 * calibration, a part replaced or a note; an org_admin of the machine's
 * organisation a note only. An intervention may name an incident of the
 * machine: that is what reopens its programmed sessions after a drive fault
 * the console cannot reset, once the incident is closed too.
 */
export const addEntry = mutation({
  args: {
    machineId: v.id("machines"),
    kind: logbookKindValidator,
    summary: v.string(),
    details: v.optional(v.string()),
    at: v.optional(v.number()),
    incidentId: v.optional(v.id("incidents")),
    storageId: v.optional(v.id("_storage")),
  },
  returns: v.id("machine_logbook"),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);
    const machine = await ctx.db.get(args.machineId);
    if (
      machine === null ||
      machine.isDeleted ||
      !inScope(caller, machine.organizationId)
    )
      throw refusal("machine_not_found");
    if (!writable(caller, machine.organizationId).includes(args.kind))
      throw refusal("logbook_kind_not_allowed");
    if (args.incidentId !== undefined && args.kind !== "intervention")
      throw refusal("invalid_logbook_entry");
    const now = Date.now();
    // Not in the future, nor before the machine existed.
    if (
      args.at !== undefined &&
      (!Number.isFinite(args.at) || args.at > now || args.at < machine._creationTime)
    )
      throw refusal("invalid_logbook_entry");
    const details =
      args.details === undefined || args.details.trim() === ""
        ? undefined
        : line(args.details, LOGBOOK_DETAILS_MAX);
    const entryId = await writeLogbook(ctx, machine, {
      kind: args.kind,
      summary: line(args.summary, LOGBOOK_SUMMARY_MAX),
      details,
      by: caller._id,
      at: args.at ?? now,
      refs: {
        incidentId:
          args.incidentId === undefined
            ? undefined
            : await machineIncident(ctx, machine, args.incidentId),
        storageId:
          args.storageId === undefined
            ? undefined
            : await attachment(ctx, args.storageId),
      },
    });
    if (args.incidentId !== undefined)
      await refreshInterventionDue(ctx, args.incidentId);
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: machine.organizationId,
      action: "logbook.entry_added",
      targetId: machine._id,
      details: { fields: [args.kind] },
    });
    return entryId;
  },
});
