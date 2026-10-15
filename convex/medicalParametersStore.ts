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
import {
  pendingSessionTtlMs,
  settlePendingLaunchesForRegistration,
} from "./training";

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
  returns: v.union(v.string(), v.object({ refusal: v.string() })),
  handler: async (ctx, args) => {
    const now = Date.now();
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

    const pendingCount = await settlePendingLaunchesForRegistration(ctx, now);
    if (pendingCount > 0) {
      const singular = pendingCount === 1;
      const noun = singular ? "launch" : "launches";
      const verb = singular ? "is" : "are";
      const pronoun = singular ? "it is" : "they are";
      return {
        refusal: `Cannot register a medical parameter version while ${pendingCount} remote ${noun} ${verb} pending; retry once ${pronoun} armed, failed or expired (pending timeout: ${pendingSessionTtlMs() / 1000} s).`,
      };
    }

    await ctx.db.insert("medical_parameters_versions", {
      ...args,
      registeredAt: Date.now(),
    });
    return args.version;
  },
});
