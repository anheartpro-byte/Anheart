import { v } from "convex/values";
import { mutation, query } from "./_generated/server";
import type { Id } from "./_generated/dataModel";
import {
  CURRENT_QUESTIONNAIRE,
  LATEST_FITNESS_MILLIS,
  computeOutcome,
  maxValidUntil,
} from "../lib/fitnessQuestionnaire";
import { CONSENT_TEXT_VERSION } from "../lib/consentText";
import {
  canManageUser,
  getCurrentUserOrThrow,
  inScope,
  organizationRoleOf,
} from "./lib/auth";
import {
  activeConsent,
  latestAssessment,
  riderLaunchRefusal,
  riderOrganizationId,
} from "./lib/fitness";
import { refusal } from "./lib/errors";
import { limitUser } from "./lib/rateLimits";
import { audit, userActor } from "./lib/audit";

const fitnessOutcomeValidator = v.union(
  v.literal("fit"),
  v.literal("unfit"),
  v.literal("needs_medical_opinion"),
);

const readinessFitnessValidator = v.union(
  v.object({
    assessmentId: v.id("fitness_assessments"),
    questionnaireVersion: v.string(),
    outcome: fitnessOutcomeValidator,
    submittedAt: v.number(),
    submittedByName: v.string(),
    validated: v.boolean(),
    validatedAt: v.optional(v.number()),
    validatedByName: v.optional(v.string()),
    validUntil: v.optional(v.number()),
    current: v.boolean(),
    answers: v.optional(v.record(v.string(), v.boolean())),
    notes: v.optional(v.string()),
  }),
  v.null(),
);

/**
 * Store the caller's questionnaire answers, or a managed rider's answers.
 * Refuses an unregistered rider, an unmanaged target, an obsolete version,
 * or answers that do not match the current questionnaire.
 */
export const submitFitnessAnswers = mutation({
  args: {
    userId: v.optional(v.id("users")),
    questionnaireVersion: v.string(),
    answers: v.record(v.string(), v.boolean()),
  },
  returns: v.object({
    assessmentId: v.id("fitness_assessments"),
    outcome: fitnessOutcomeValidator,
  }),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    const userId = args.userId ?? caller._id;
    if (userId !== caller._id && !(await canManageUser(ctx, userId, caller))) {
      throw refusal("user_not_managed");
    }
    const organizationId = await riderOrganizationId(ctx, userId, caller);
    if (organizationId === null) throw refusal("user_not_managed");

    const computed = computeOutcome(
      CURRENT_QUESTIONNAIRE,
      args.questionnaireVersion,
      args.answers,
    );
    if (!computed.ok) {
      throw refusal(
        computed.reason === "version_mismatch"
          ? "questionnaire_outdated"
          : "fitness_answers_invalid",
      );
    }

    const assessmentId = await ctx.db.insert("fitness_assessments", {
      userId,
      organizationId,
      questionnaireVersion: args.questionnaireVersion,
      answers: args.answers,
      outcome: computed.outcome,
      submittedBy: caller._id,
      submittedAt: Date.now(),
    });
    return { assessmentId, outcome: computed.outcome };
  },
});

/**
 * Validate the latest assessment for a rider the caller manages.
 * Refuses a missing, out-of-scope, superseded, already validated, obsolete,
 * or self assessment, and invalid validity or notes.
 */
export const validateFitnessAssessment = mutation({
  args: {
    assessmentId: v.id("fitness_assessments"),
    validUntil: v.optional(v.number()),
    notes: v.optional(v.string()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    const assessment = await ctx.db.get(args.assessmentId);
    if (assessment === null || !inScope(caller, assessment.organizationId)) {
      throw refusal("fitness_assessment_not_found");
    }
    if (caller._id === assessment.userId) {
      throw refusal("fitness_self_validation");
    }
    if (!(await canManageUser(ctx, assessment.userId, caller))) {
      throw refusal("user_not_managed");
    }
    const latest = await latestAssessment(
      ctx,
      assessment.userId,
      assessment.organizationId,
    );
    if (latest === null || latest._id !== assessment._id) {
      throw refusal("fitness_assessment_superseded");
    }
    if (
      assessment.validatedBy !== undefined ||
      assessment.validatedAt !== undefined
    ) {
      throw refusal("fitness_already_validated");
    }
    if (assessment.questionnaireVersion !== CURRENT_QUESTIONNAIRE.version) {
      throw refusal("questionnaire_outdated");
    }

    const now = Date.now();
    const notes = args.notes?.trim();
    if (notes !== undefined && notes.length > 1000) {
      throw refusal("fitness_notes_too_long");
    }
    if (assessment.outcome === "fit") {
      const maximum = maxValidUntil(CURRENT_QUESTIONNAIRE, now);
      if (
        args.validUntil === undefined ||
        !Number.isFinite(args.validUntil) ||
        !Number.isInteger(args.validUntil) ||
        Number.isNaN(new Date(args.validUntil).getTime()) ||
        args.validUntil <= now ||
        args.validUntil > LATEST_FITNESS_MILLIS ||
        (maximum !== null && args.validUntil > maximum)
      ) {
        throw refusal("invalid_validity");
      }
    } else if (args.validUntil !== undefined) {
      throw refusal("invalid_validity");
    }

    await ctx.db.patch(args.assessmentId, {
      validatedBy: caller._id,
      validatedAt: now,
      validUntil: args.validUntil,
      notes: notes === "" ? undefined : notes,
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId: assessment.organizationId,
      action: "fitness.validated",
      targetId: args.assessmentId,
      details: { userId: assessment.userId },
    });
    return null;
  },
});

/**
 * Sign the current consent text for the caller in their active organisation.
 * Any active member may sign for themselves; refuses an inactive membership
 * or an obsolete text version. There is no argument for signing for another.
 */
export const signConsent = mutation({
  args: { textVersion: v.string() },
  returns: v.id("consents"),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    if (args.textVersion !== CONSENT_TEXT_VERSION) {
      throw refusal("consent_text_outdated");
    }
    const organizationId = await riderOrganizationId(ctx, caller._id, caller);
    if (organizationId === null) throw refusal("not_authorized");
    const existing = await activeConsent(ctx, caller._id, organizationId);
    if (existing !== null) return existing._id;
    const consentId = await ctx.db.insert("consents", {
      userId: caller._id,
      organizationId,
      textVersion: args.textVersion,
      signedAt: Date.now(),
      signedVia: "site",
    });
    await audit(ctx, {
      actor: userActor(caller),
      source: "site",
      organizationId,
      action: "consent.signed",
      targetId: consentId,
    });
    return consentId;
  },
});

/**
 * Withdraw the caller's consent, or consent for a rider the caller manages.
 * Any active member may withdraw their own consent; refuses an inactive
 * membership, an unmanaged target, or when the target has no active consent.
 */
export const withdrawConsent = mutation({
  args: { userId: v.optional(v.id("users")) },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await getCurrentUserOrThrow(ctx);
    const userId = args.userId ?? caller._id;
    if (userId !== caller._id && !(await canManageUser(ctx, userId, caller))) {
      throw refusal("user_not_managed");
    }
    const organizationId = await riderOrganizationId(ctx, userId, caller);
    if (organizationId === null) {
      throw refusal(userId === caller._id ? "not_authorized" : "user_not_managed");
    }
    const consents = await ctx.db
      .query("consents")
      .withIndex("by_user_and_organization", (q) =>
        q.eq("userId", userId).eq("organizationId", organizationId),
      )
      .filter((q) => q.eq(q.field("withdrawnAt"), undefined))
      .collect();
    if (consents.length === 0) throw refusal("consent_not_active");
    const now = Date.now();
    for (const consent of consents) {
      await ctx.db.patch(consent._id, {
        withdrawnAt: now,
        withdrawnBy: caller._id,
      });
      await audit(ctx, {
        actor: userActor(caller),
        source: "site",
        organizationId: consent.organizationId,
        action: "consent.withdrawn",
        targetId: consent._id,
      });
    }
    return null;
  },
});

/**
 * Return a rider's assessment and consent status to themselves or an
 * authorized manager. Returns null outside that scope; answers and review
 * notes are withheld from the rider and all console-facing calls.
 */
export const getRiderReadiness = query({
  args: {
    userId: v.id("users"),
    machineId: v.optional(v.id("machines")),
  },
  returns: v.union(
    v.object({
      serverNow: v.number(),
      questionnaireVersion: v.string(),
      consentTextVersion: v.string(),
      fitness: readinessFitnessValidator,
      consent: v.object({
        active: v.union(
          v.object({
            signedAt: v.number(),
            textVersion: v.string(),
            signedVia: v.union(v.literal("site"), v.literal("console")),
          }),
          v.null(),
        ),
        latestSigned: v.union(
          v.object({ textVersion: v.string(), signedAt: v.number() }),
          v.null(),
        ),
        lastWithdrawnAt: v.optional(v.number()),
      }),
      launchRefusal: v.union(
        v.literal("rider_not_fit"),
        v.literal("fitness_expired"),
        v.literal("consent_missing"),
        v.null(),
      ),
      canValidate: v.boolean(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const caller = await getCurrentUserOrThrow(ctx);
    let organizationId: Id<"organizations"> | null;
    if (args.machineId !== undefined) {
      const machine = await ctx.db.get(args.machineId);
      if (
        machine === null ||
        machine.isDeleted === true ||
        !inScope(caller, machine.organizationId) ||
        machine.organizationId === undefined
      ) {
        return null;
      }
      organizationId = machine.organizationId;
      if (
        (await organizationRoleOf(ctx, args.userId, organizationId)) === null
      ) {
        return null;
      }
    } else {
      organizationId = await riderOrganizationId(ctx, args.userId, caller);
      if (organizationId === null) return null;
    }

    const manages = await canManageUser(ctx, args.userId, caller);
    if (caller._id !== args.userId && !manages) return null;
    const assessment = await latestAssessment(ctx, args.userId, organizationId);
    const consent = await activeConsent(ctx, args.userId, organizationId);
    const latestSigned = await ctx.db
      .query("consents")
      .withIndex("by_user_and_organization", (q) =>
        q.eq("userId", args.userId).eq("organizationId", organizationId),
      )
      .filter((q) => q.eq(q.field("withdrawnAt"), undefined))
      .order("desc")
      .first();
    const withdrawnConsent = await ctx.db
      .query("consents")
      .withIndex("by_user_and_organization", (q) =>
        q.eq("userId", args.userId).eq("organizationId", organizationId),
      )
      .filter((q) => q.neq(q.field("withdrawnAt"), undefined))
      .order("desc")
      .first();
    const serverNow = Date.now();
    let fitness: null | {
      assessmentId: Id<"fitness_assessments">;
      questionnaireVersion: string;
      outcome: "fit" | "unfit" | "needs_medical_opinion";
      submittedAt: number;
      submittedByName: string;
      validated: boolean;
      validatedAt?: number;
      validatedByName?: string;
      validUntil?: number;
      current: boolean;
      answers?: Record<string, boolean>;
      notes?: string;
    } = null;
    if (assessment !== null) {
      const submittedBy = await ctx.db.get(assessment.submittedBy);
      const validatedBy =
        assessment.validatedBy === undefined
          ? null
          : await ctx.db.get(assessment.validatedBy);
      fitness = {
        assessmentId: assessment._id,
        questionnaireVersion: assessment.questionnaireVersion,
        outcome: assessment.outcome,
        submittedAt: assessment.submittedAt,
        submittedByName: submittedBy
          ? `${submittedBy.firstName} ${submittedBy.lastName}`.trim()
          : "",
        validated: assessment.validatedAt !== undefined,
        validatedAt: assessment.validatedAt,
        validatedByName: validatedBy
          ? `${validatedBy.firstName} ${validatedBy.lastName}`.trim()
          : undefined,
        validUntil: assessment.validUntil,
        current:
          assessment.questionnaireVersion === CURRENT_QUESTIONNAIRE.version,
        answers:
          manages && caller._id !== args.userId
            ? assessment.answers
            : undefined,
        notes:
          manages && caller._id !== args.userId ? assessment.notes : undefined,
      };
    }
    const active = consent
      ? {
          signedAt: consent.signedAt,
          textVersion: consent.textVersion,
          signedVia: consent.signedVia,
        }
      : null;
    return {
      serverNow,
      questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
      consentTextVersion: CONSENT_TEXT_VERSION,
      fitness,
      consent: {
        active,
        latestSigned: latestSigned
          ? {
              textVersion: latestSigned.textVersion,
              signedAt: latestSigned.signedAt,
            }
          : null,
        lastWithdrawnAt: withdrawnConsent?.withdrawnAt,
      },
      launchRefusal: await riderLaunchRefusal(
        ctx,
        args.userId,
        organizationId,
        serverNow,
      ),
      canValidate: manages && caller._id !== args.userId,
    };
  },
});
