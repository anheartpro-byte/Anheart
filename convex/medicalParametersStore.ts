/**
 * Enforce the append-only registry for verified medical parameter versions.
 * The accepted schema and bounds mirror `raspberry-pi/src/medical.py`,
 * `raspberry-pi/src/local_config.py`, and `raspberry-pi/src/training/motion.py`.
 * Keeping every signed version preserves the audit trail; current is the
 * greatest semantic version and older rows are never replaced or deleted.
 */
import { v } from "convex/values";
import { internalMutation } from "./_generated/server";
import { compareMedicalVersions, currentMedicalParameters } from "./lib/medicalParameters";
import { expired, pendingSessionTtlMs } from "./training";

const registrationValues = {
  version: v.string(),
  issuedAt: v.string(),
  storageId: v.id("_storage"),
  signedBy: v.string(),
  signature: v.string(),
  zoneCeilingFraction: v.number(),
  minRiderAge: v.number(),
  hrMaxFormula: v.literal("tanaka"),
};

/** Add a unique version that is newer than the current registered version. */
export const insertVersion = internalMutation({
  args: registrationValues,
  returns: v.string(),
  handler: async (ctx, args) => {
    const now = Date.now();
    const recentPending = ctx.db
      .query("sessions")
      .withIndex("by_status_and_started", (q) =>
        q.eq("status", "pending").gte("startedAt", now - pendingSessionTtlMs()),
      )
      .filter((q) => q.eq(q.field("origin"), "remote"));
    let pendingCount = 0;
    for await (const session of recentPending) {
      if (!expired(session, now)) pendingCount += 1;
    }
    if (pendingCount > 0) {
      const plural = pendingCount === 1 ? "" : "es";
      const retry = pendingCount === 1 ? "it arms or expires" : "they arm or expire";
      throw new Error(
        `Cannot register a medical parameter version while ${pendingCount} remote pending launch${plural}; retry after ${retry}. Their timeout is ${pendingSessionTtlMs() / 1000} seconds (300 by default, 600 maximum).`,
      );
    }

    const duplicate = await ctx.db
      .query("medical_parameters_versions")
      .withIndex("by_version", (q) => q.eq("version", args.version))
      .unique();
    if (duplicate !== null) {
      throw new Error(`Medical parameters version ${args.version} is already registered`);
    }
    const current = await currentMedicalParameters(ctx);
    if (
      current !== null &&
      compareMedicalVersions(args.version, current.version) <= 0
    ) {
      throw new Error(
        `Medical parameters version ${args.version} is not greater than current version ${current.version}`,
      );
    }
    await ctx.db.insert("medical_parameters_versions", {
      ...args,
      registeredAt: Date.now(),
    });
    return args.version;
  },
});
