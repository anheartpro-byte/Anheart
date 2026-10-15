import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import { CURRENT_QUESTIONNAIRE } from "../../lib/fitnessQuestionnaire";
import { CONSENT_TEXT_VERSION } from "../../lib/consentText";
import { organizationRoleOf, type CurrentUser } from "./auth";

type Ctx = QueryCtx | MutationCtx;

export type FitnessLaunchRefusal =
  | "rider_not_fit"
  | "fitness_expired"
  | "consent_missing";

export async function riderOrganizationId(
  ctx: Ctx,
  userId: Id<"users">,
  caller: CurrentUser,
): Promise<Id<"organizations"> | null> {
  let organizationId: Id<"organizations"> | undefined = caller.organizationId;
  if (caller.role === "admin" && caller._id !== userId) {
    organizationId = (await ctx.db.get(userId))?.organizationId;
  }
  if (
    organizationId === undefined ||
    (await organizationRoleOf(ctx, userId, organizationId)) === null
  ) {
    return null;
  }
  return organizationId;
}

export async function latestAssessment(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
): Promise<Doc<"fitness_assessments"> | null> {
  return await ctx.db
    .query("fitness_assessments")
    .withIndex("by_user_and_organization", (q) =>
      q.eq("userId", userId).eq("organizationId", organizationId),
    )
    .order("desc")
    .first();
}

export async function activeConsent(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
): Promise<Doc<"consents"> | null> {
  return await ctx.db
    .query("consents")
    .withIndex("by_user_and_organization", (q) =>
      q.eq("userId", userId).eq("organizationId", organizationId),
    )
    .filter((q) =>
      q.and(
        q.eq(q.field("textVersion"), CONSENT_TEXT_VERSION),
        q.eq(q.field("withdrawnAt"), undefined),
      ),
    )
    .order("desc")
    .first();
}

export async function riderLaunchRefusal(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
  now: number,
): Promise<FitnessLaunchRefusal | null> {
  const assessment = await latestAssessment(ctx, userId, organizationId);
  if (
    assessment === null ||
    assessment.questionnaireVersion !== CURRENT_QUESTIONNAIRE.version ||
    assessment.outcome !== "fit" ||
    assessment.validatedAt === undefined
  ) {
    return "rider_not_fit";
  }
  if (assessment.validUntil === undefined || assessment.validUntil <= now) {
    return "fitness_expired";
  }
  if ((await activeConsent(ctx, userId, organizationId)) === null) {
    return "consent_missing";
  }
  return null;
}
