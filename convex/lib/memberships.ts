/**
 * Every write of a `memberships` row goes through here (ANH-152).
 *
 * A membership that is no longer active, or whose role is no longer one an
 * operator may hold (admin, gestionnaire), releases that member's console
 * operator rows in its organisation (`releaseOperator`): disabled, their code
 * cleared, so that coming back takes a new activation and a new code. Clerk's
 * webhook, the role an admin sets, the mirror of the sign-in token, the
 * invitations, the account deletion and the migrations all write memberships
 * with these three functions, so a new path cannot forget the release.
 * `memberships.test.ts` pins that no other module writes the table.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx } from "../_generated/server";
import { OPERATOR_ROLES, releaseOperator } from "./consoleAccess";

type MembershipFields = Omit<Doc<"memberships">, "_id" | "_creationTime">;

/** Release the member's operator rows when this membership no longer lets them be one. */
async function releaseUnlessOperator(
  ctx: MutationCtx,
  membership: Pick<MembershipFields, "userId" | "organizationId" | "role" | "active">,
  at: number,
): Promise<void> {
  if (!membership.active || !OPERATOR_ROLES.includes(membership.role))
    await releaseOperator(ctx, membership.userId, membership.organizationId, at);
}

/** A new membership. `at`: when the change took place (Clerk's date, else now). */
export async function insertMembership(
  ctx: MutationCtx,
  fields: MembershipFields,
  at: number = Date.now(),
): Promise<Id<"memberships">> {
  const id = await ctx.db.insert("memberships", fields);
  await releaseUnlessOperator(ctx, fields, at);
  return id;
}

/** A change of a membership: its role, its state, its organisation, its Clerk identifiers. */
export async function updateMembership(
  ctx: MutationCtx,
  membership: Doc<"memberships">,
  change: Partial<MembershipFields>,
  at: number = Date.now(),
): Promise<void> {
  await ctx.db.patch(membership._id, change);
  await releaseUnlessOperator(ctx, { ...membership, ...change }, at);
}

/** A membership removed: the member is out of that organisation. */
export async function deleteMembership(
  ctx: MutationCtx,
  membership: Doc<"memberships">,
  at: number = Date.now(),
): Promise<void> {
  await ctx.db.delete(membership._id);
  await releaseOperator(ctx, membership.userId, membership.organizationId, at);
}
