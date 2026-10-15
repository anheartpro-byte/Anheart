import { v } from "convex/values";
import { internal } from "./_generated/api";
import { internalAction, query } from "./_generated/server";
import type { Id } from "./_generated/dataModel";
import { getCurrentUserOrThrow } from "./lib/auth";
import {
  currentMedicalParameters,
  MEDICAL_PUBLIC_KEY_PEM,
  parseMedicalDocument,
  verifyMedicalParametersSignature,
} from "./lib/medicalParameters";

const publicVersionValidator = v.object({
  version: v.string(),
  issuedAt: v.string(),
  signedBy: v.string(),
  zoneCeilingFraction: v.number(),
  minRiderAge: v.number(),
  hrMaxFormula: v.literal("tanaka"),
});

export const current = query({
  args: {},
  returns: v.union(publicVersionValidator, v.null()),
  handler: async (ctx) => {
    await getCurrentUserOrThrow(ctx);
    const version = await currentMedicalParameters(ctx);
    if (version === null) return null;
    return {
      version: version.version,
      issuedAt: version.issuedAt,
      signedBy: version.signedBy,
      zoneCeilingFraction: version.zoneCeilingFraction,
      minRiderAge: version.minRiderAge,
      hrMaxFormula: version.hrMaxFormula,
    };
  },
});

export const registerVersion = internalAction({
  args: { content: v.string(), signatureBase64: v.string() },
  returns: v.string(),
  handler: async (ctx, args): Promise<string> => {
    if (
      !(await verifyMedicalParametersSignature(
        args.content,
        args.signatureBase64,
        MEDICAL_PUBLIC_KEY_PEM,
      ))
    ) {
      throw new Error("Medical parameters signature is missing or invalid");
    }

    const parsed = parseMedicalDocument(args.content);
    const storageId: Id<"_storage"> = await ctx.storage.store(
      new Blob([args.content], { type: "application/json" }),
    );
    try {
      return await ctx.runMutation(internal.medicalParametersStore.insertVersion, {
        version: parsed.version,
        issuedAt: parsed.issuedAt,
        storageId,
        signedBy: parsed.signedBy,
        signature: args.signatureBase64,
        zoneCeilingFraction: parsed.zoneCeilingFraction,
        minRiderAge: parsed.minRiderAge,
        hrMaxFormula: parsed.hrMaxFormula,
      });
    } catch (error) {
      await ctx.storage.delete(storageId);
      throw error;
    }
  },
});
