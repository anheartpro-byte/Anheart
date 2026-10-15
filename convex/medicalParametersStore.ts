import { v } from "convex/values";
import { internalMutation } from "./_generated/server";
import { compareMedicalVersions, currentMedicalParameters } from "./lib/medicalParameters";

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

export const insertVersion = internalMutation({
  args: registrationValues,
  returns: v.string(),
  handler: async (ctx, args) => {
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
