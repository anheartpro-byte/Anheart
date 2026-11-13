/**
 * The programme catalogue (ANH-137): written by the Anheart admin alone, read
 * by everyone once published (docs/programmes.md).
 *
 * draft -> validated (by an admin who did not write it) -> published ->
 * retired. Every write runs the Pi's validation (`convex/lib/programmes.ts`)
 * and is audited. Publishing to the machines is ANH-138.
 */
import type { WithoutSystemFields } from "convex/server";
import { ConvexError, v } from "convex/values";
import { mutation, query, type MutationCtx } from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import schema from "./schema";
import { getCurrentUserOrThrow, requireRole } from "./lib/auth";
import { audit, userActor, type AuditAction } from "./lib/audit";
import { refusal } from "./lib/errors";
import { currentMedicalParameters } from "./lib/medicalParameters";
import {
  PROGRAMME_MEDICAL_VERSION,
  PROGRAMME_SCHEMA_VERSION,
  programmeProfileFields,
  programmeRefusal,
  type ProgrammeProfile,
} from "./lib/programmes";
import { limitUser } from "./lib/rateLimits";

const programmeRow = schema
  .doc("programmes")
  .extend({ simulationReportUrl: v.union(v.string(), v.null()) });

/** Every programme to the Anheart admin; the published catalogue to anyone else. */
export const list = query({
  args: {},
  returns: v.array(programmeRow),
  handler: async (ctx) => {
    const caller = await getCurrentUserOrThrow(ctx);
    // shortcut: whole catalogue in one read, written by hand (tens of rows); paginate past a few thousand.
    if (caller.role !== "admin") {
      const published = await ctx.db
        .query("programmes")
        .withIndex("by_status", (q) => q.eq("status", "published"))
        .collect();
      return published
        .filter((programme) => programme.organizationId === null)
        .map((programme) => ({ ...programme, simulationReportUrl: null }));
    }
    const programmes = await ctx.db.query("programmes").collect();
    return await Promise.all(
      programmes.map(async (programme) => ({
        ...programme,
        simulationReportUrl:
          programme.simulationReport === null
            ? null
            : await ctx.storage.getUrl(programme.simulationReport),
      })),
    );
  },
});

/** The Pi's verdict, thrown with its own message when it refuses. */
function requireValid(profile: ProgrammeProfile): void {
  const refused = programmeRefusal(profile);
  if (refused !== null)
    throw new ConvexError({
      code: "programme_refused",
      message: refused.detail,
      violations: refused.violations,
    });
}

/** A profile id keys session records on the machines: one programme each. */
async function requireFreeProfileId(
  ctx: MutationCtx,
  profileId: string,
  self: Id<"programmes"> | null,
): Promise<void> {
  const holder = await ctx.db
    .query("programmes")
    .withIndex("by_profile_id", (q) => q.eq("profileId", profileId))
    .first();
  if (holder !== null && holder._id !== self) throw refusal("programme_id_taken");
}

/** The Anheart admin, and the programme when it is in `status`. */
async function programmeIn(
  ctx: MutationCtx,
  programmeId: Id<"programmes">,
  status: Doc<"programmes">["status"],
) {
  await limitUser(ctx);
  const admin = await requireRole(ctx, ["admin"]);
  const programme = await ctx.db.get(programmeId);
  if (programme === null) throw refusal("programme_not_found");
  if (programme.status !== status) throw refusal("programme_wrong_status");
  return { admin, programme };
}

async function audited(
  ctx: MutationCtx,
  admin: Awaited<ReturnType<typeof requireRole>>,
  programme: Pick<Doc<"programmes">, "_id" | "organizationId">,
  action: AuditAction,
  details?: Parameters<typeof audit>[1]["details"],
) {
  await audit(ctx, {
    actor: userActor(admin),
    source: "site",
    organizationId: programme.organizationId ?? undefined,
    action,
    targetId: programme._id,
    details,
  });
}

/** A new draft of the Anheart catalogue; duplicating is creating from a copy. */
export const create = mutation({
  args: programmeProfileFields,
  returns: v.id("programmes"),
  handler: async (ctx, profile) => {
    await limitUser(ctx);
    const admin = await requireRole(ctx, ["admin"]);
    requireValid(profile);
    await requireFreeProfileId(ctx, profile.profileId, null);
    const now = Date.now();
    const programmeId = await ctx.db.insert("programmes", {
      ...profile,
      organizationId: null,
      status: "draft",
      schemaVersion: PROGRAMME_SCHEMA_VERSION,
      revision: 0,
      medicalParametersVersion: PROGRAMME_MEDICAL_VERSION,
      medicalSignoff: null,
      simulationReport: null,
      validatedBy: null,
      createdBy: admin._id,
      updatedBy: admin._id,
      createdAt: now,
      updatedAt: now,
    });
    await audited(ctx, admin, { _id: programmeId, organizationId: null }, "programme.created");
    return programmeId;
  },
});

/**
 * Rewrite a draft. The simulation report and the medical sign-off vouched for
 * the values they were given with, so both are cleared.
 */
export const updateDraft = mutation({
  args: { programmeId: v.id("programmes"), ...programmeProfileFields },
  returns: v.null(),
  handler: async (ctx, { programmeId, ...profile }) => {
    const { admin, programme } = await programmeIn(ctx, programmeId, "draft");
    requireValid(profile);
    await requireFreeProfileId(ctx, profile.profileId, programmeId);
    const fields = (Object.keys(profile) as (keyof ProgrammeProfile)[]).filter(
      (key) => JSON.stringify(profile[key]) !== JSON.stringify(programme[key]),
    );
    await ctx.db.patch(programmeId, {
      ...profile,
      medicalParametersVersion: PROGRAMME_MEDICAL_VERSION,
      medicalSignoff: null,
      simulationReport: null,
      updatedBy: admin._id,
      updatedAt: Date.now(),
    });
    await audited(ctx, admin, programme, "programme.updated", { fields });
    return null;
  },
});

/** Attach to a draft the report of `python -m simulation.quick --cohort --profile`. */
export const attachSimulationReport = mutation({
  args: { programmeId: v.id("programmes"), storageId: v.id("_storage") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { admin, programme } = await programmeIn(ctx, args.programmeId, "draft");
    const file = await ctx.db.system.get(args.storageId);
    if (
      file === null ||
      (file.contentType !== "application/pdf" &&
        file.contentType !== "application/json") ||
      file.size > 10 * 1024 * 1024 ||
      Date.now() - file._creationTime >= 3_600_000
    )
      throw refusal("invalid_registry_file");
    // Not `updatedBy`: attaching evidence does not make an author.
    await ctx.db.patch(programme._id, {
      simulationReport: args.storageId,
      updatedAt: Date.now(),
    });
    await audited(ctx, admin, programme, "programme.report_attached");
    return null;
  },
});

/**
 * draft -> validated, by an Anheart admin who neither created nor last wrote
 * the draft, with the medical sign-off, once the Pi's validation passes on
 * the stored values, their medical version is the current one and a
 * simulation report is attached (EX-4).
 */
export const validate = mutation({
  args: {
    programmeId: v.id("programmes"),
    medicalSignoff: v.object({ by: v.string(), at: v.number(), note: v.string() }),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { admin, programme } = await programmeIn(ctx, args.programmeId, "draft");
    if (admin._id === programme.createdBy || admin._id === programme.updatedBy)
      throw refusal("programme_self_validation");
    requireValid(programme);
    const current = await currentMedicalParameters(ctx);
    if (current === null) throw refusal("programme_medical_version_unavailable");
    if (current.version !== programme.medicalParametersVersion)
      throw refusal("programme_medical_version_not_current");
    if (programme.simulationReport === null)
      throw refusal("programme_simulation_report_missing");
    const signoff = {
      by: args.medicalSignoff.by.trim(),
      at: args.medicalSignoff.at,
      note: args.medicalSignoff.note.trim(),
    };
    const now = Date.now();
    if (
      signoff.by.length === 0 ||
      signoff.by.length > 200 ||
      signoff.note.length > 1000 ||
      !Number.isSafeInteger(signoff.at) ||
      signoff.at < Date.UTC(2024, 0, 1) ||
      signoff.at > now + 86_400_000
    )
      throw refusal("invalid_medical_signoff");
    await ctx.db.patch(programme._id, {
      status: "validated",
      medicalSignoff: signoff,
      validatedBy: admin._id,
      updatedBy: admin._id,
      updatedAt: now,
    });
    await audited(ctx, admin, programme, "programme.validated", {
      medicalParametersVersion: programme.medicalParametersVersion,
    });
    return null;
  },
});

/** validated -> published: one more revision, copied whole into the history. */
export const publish = mutation({
  args: { programmeId: v.id("programmes") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { admin, programme } = await programmeIn(ctx, args.programmeId, "validated");
    const fields = Object.fromEntries(
      Object.entries(programme).filter(([key]) => !key.startsWith("_")),
    ) as WithoutSystemFields<Doc<"programmes">>;
    const published = {
      ...fields,
      status: "published" as const,
      revision: programme.revision + 1,
      updatedBy: admin._id,
      updatedAt: Date.now(),
    };
    await ctx.db.replace(programme._id, published);
    await ctx.db.insert("programme_revisions", {
      programmeId: programme._id,
      ...published,
    });
    await audited(ctx, admin, programme, "programme.published");
    return null;
  },
});

/** published -> retired: no longer listed nor launchable; sessions keep their copy. */
export const retire = mutation({
  args: { programmeId: v.id("programmes") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const { admin, programme } = await programmeIn(ctx, args.programmeId, "published");
    await ctx.db.patch(programme._id, {
      status: "retired",
      updatedBy: admin._id,
      updatedAt: Date.now(),
    });
    await audited(ctx, admin, programme, "programme.retired");
    return null;
  },
});
