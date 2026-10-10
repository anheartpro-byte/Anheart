/**
 * Who is calling, in which organisation, with which role.
 *
 * The organisation and the role of a call come from the verified identity
 * (`ctx.auth.getUserIdentity()`, claims `org_id` and `org_role` of the Clerk
 * JWT template `convex`) and from server-side rows that no public function
 * lets a user write. They never come from an argument.
 *
 * Every helper below answers for ONE organisation: a resource of another
 * organisation is refused exactly like a resource that does not exist. The
 * only caller that crosses organisations is an admin of the Anheart
 * organisation (role "admin").
 *
 * The `can...` helpers take the caller that `getCurrentUserOrThrow` or
 * `requireRole` returned: a rule is never evaluated without a resolved caller.
 */
import { ConvexError } from "convex/values";
import type { UserIdentity } from "convex/server";
import { QueryCtx, MutationCtx } from "../_generated/server";
import { Doc, Id } from "../_generated/dataModel";

type Ctx = QueryCtx | MutationCtx;

/** Role inside one organisation: mirror of the Clerk organisation roles. */
export type OrganizationRole = "admin" | "gestionnaire" | "user";

/**
 * Role a call is made with.
 *
 * - "admin": admin of the Anheart organisation. Every organisation.
 * - "org_admin": admin of any other organisation. That organisation only.
 * - "gestionnaire", "user": inside their organisation, through their links.
 *
 * "admin" always means the Anheart admin, so a rule written for "admin" alone
 * never opens another organisation to an organisation admin.
 */
export type Role = "admin" | "org_admin" | "gestionnaire" | "user";

/** Admin of an organisation: Anheart's (everywhere) or the caller's own. */
export const ORGANIZATION_ADMIN_ROLES: Role[] = ["admin", "org_admin"];
/** Everyone who manages machines and patients. */
export const STAFF_ROLES: Role[] = ["admin", "org_admin", "gestionnaire"];

/** Slug of the organisation the migration attaches existing rows to. */
export const DEFAULT_ORGANIZATION_SLUG = "anheart";

export const NO_ACTIVE_ORGANIZATION =
  "No active organization: select an organization to continue";
export const ORGANIZATION_NOT_SYNCHRONIZED =
  "This organization is not known to the server yet";
export const UNKNOWN_ORGANIZATION_ROLE =
  "Your role in this organization is not recognized";
export const MALFORMED_ORGANIZATION_CLAIM = "Malformed organization claim";
export const NO_ORGANIZATION =
  "Your account does not belong to an organization";

/**
 * The user row of the caller, with the role and the organisation of the
 * verified session in place of the stored mirrors (`users.role`,
 * `users.organizationId`), so that no rule can read a mirror by mistake.
 */
export type CurrentUser = Omit<Doc<"users">, "role" | "organizationId"> & {
  role: Role;
  organizationId: Id<"organizations">;
};

type Session = { organization: Doc<"organizations">; role: Role };

/** What the organisation claims of a verified identity designate. */
export type ClaimedOrganization =
  | {
      kind: "claimed";
      organization: Doc<"organizations">;
      organizationRole: OrganizationRole;
    }
  /** No organisation claim, and the deployment still accepts that. */
  | { kind: "unclaimed" }
  | { kind: "refused"; reason: string };

/** Clerk roles, with or without the `org:` prefix of their key. */
const ORGANIZATION_ROLE_OF_CLAIM = new Map<string, OrganizationRole>([
  ["admin", "admin"],
  ["gestionnaire", "gestionnaire"],
  ["patient", "user"],
]);

/**
 * Clerk identifier of the Anheart organisation, from the deployment
 * environment variable `ANHEART_ORG_ID`. Null until the product owner has
 * configured Clerk Organizations for this deployment.
 */
export function anheartClerkOrganizationId(): string | null {
  const value = (process.env.ANHEART_ORG_ID ?? "").trim();
  return value === "" ? null : value;
}

/**
 * True for the one organisation whose admins are Anheart admins: the Clerk
 * organisation named by `ANHEART_ORG_ID`. Until that variable is set it is the
 * default organisation created by the migration, not yet linked to Clerk.
 */
export function isAnheartOrganization(
  organization: Pick<Doc<"organizations">, "clerkOrgId" | "slug">,
): boolean {
  const configured = anheartClerkOrganizationId();
  if (configured !== null) return organization.clerkOrgId === configured;
  return (
    organization.clerkOrgId === undefined &&
    organization.slug === DEFAULT_ORGANIZATION_SLUG
  );
}

/** The role a member acts with, from the role they hold in the organisation. */
export function effectiveRole(
  organization: Pick<Doc<"organizations">, "clerkOrgId" | "slug">,
  organizationRole: OrganizationRole,
): Role {
  if (organizationRole !== "admin") return organizationRole;
  return isAnheartOrganization(organization) ? "admin" : "org_admin";
}

/** `org:admin`, `org:gestionnaire`, `org:patient`; anything else is null. */
export function organizationRoleFromClaim(
  claim: unknown,
): OrganizationRole | null {
  if (typeof claim !== "string") return null;
  const key = claim.startsWith("org:") ? claim.slice(4) : claim;
  return ORGANIZATION_ROLE_OF_CLAIM.get(key) ?? null;
}

/**
 * Require authentication - throws if not logged in
 */
export async function requireAuth(ctx: Ctx) {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) {
    throw new Error("Not authenticated");
  }
  return identity;
}

/**
 * The organisation and role carried by the verified identity.
 *
 * A token without an active organisation (`org_id` absent, null or empty) is
 * accepted only while `ANHEART_ORG_ID` is unset, that is before Clerk
 * Organizations is configured for the deployment; the caller is then resolved
 * from the server-side mirror (see `sessionOf`). An organisation the server
 * does not know, or a role it does not recognise, is refused.
 */
export async function organizationFromClaims(
  ctx: Ctx,
  identity: UserIdentity,
): Promise<ClaimedOrganization> {
  const claim = identity.org_id;
  if (claim === undefined || claim === null || claim === "") {
    return anheartClerkOrganizationId() === null
      ? { kind: "unclaimed" }
      : { kind: "refused", reason: NO_ACTIVE_ORGANIZATION };
  }
  if (typeof claim !== "string") {
    return { kind: "refused", reason: MALFORMED_ORGANIZATION_CLAIM };
  }
  const organization = await ctx.db
    .query("organizations")
    .withIndex("by_clerk_org_id", (q) => q.eq("clerkOrgId", claim))
    .unique();
  if (!organization) {
    return { kind: "refused", reason: ORGANIZATION_NOT_SYNCHRONIZED };
  }
  const organizationRole = organizationRoleFromClaim(identity.org_role);
  if (organizationRole === null) {
    return { kind: "refused", reason: UNKNOWN_ORGANIZATION_ROLE };
  }
  return { kind: "claimed", organization, organizationRole };
}

/** The membership row of a user in an organisation, active or not. */
export async function findMembership(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
): Promise<Doc<"memberships"> | null> {
  return await ctx.db
    .query("memberships")
    .withIndex("by_user_and_organization", (q) =>
      q.eq("userId", userId).eq("organizationId", organizationId),
    )
    .unique();
}

/**
 * Role of an ACTIVE member of the organisation; null for anyone else, and for
 * a row that has no organisation yet (nobody is a member of "no organisation").
 */
export async function organizationRoleOf(
  ctx: Ctx,
  userId: Id<"users">,
  organizationId: Id<"organizations"> | undefined,
): Promise<OrganizationRole | null> {
  if (organizationId === undefined) return null;
  const membership = await findMembership(ctx, userId, organizationId);
  return membership?.active ? membership.role : null;
}

/**
 * The default organisation created by the migration, while it is not linked to
 * a Clerk organisation. Null once it is linked, or before the migration.
 */
export async function findUnlinkedDefaultOrganization(
  ctx: Ctx,
): Promise<Doc<"organizations"> | null> {
  const candidates = await ctx.db
    .query("organizations")
    .withIndex("by_slug", (q) => q.eq("slug", DEFAULT_ORGANIZATION_SLUG))
    .collect();
  return (
    candidates.find((organization) => organization.clerkOrgId === undefined) ??
    null
  );
}

/**
 * The organisation and role this call is made with, or why it is refused.
 *
 * With organisation claims: the claims decide. Without them, and only while
 * the deployment accepts it: the main organisation of the account
 * (`users.organizationId`) with the role of its active membership, both
 * written by the server alone.
 */
async function sessionOf(
  ctx: Ctx,
  identity: UserIdentity,
  user: Doc<"users">,
): Promise<Session | { refusal: string }> {
  const claimed = await organizationFromClaims(ctx, identity);
  if (claimed.kind === "refused") return { refusal: claimed.reason };
  if (claimed.kind === "claimed") {
    return {
      organization: claimed.organization,
      role: effectiveRole(claimed.organization, claimed.organizationRole),
    };
  }
  const organization = user.organizationId
    ? await ctx.db.get(user.organizationId)
    : null;
  const organizationRole = await organizationRoleOf(
    ctx,
    user._id,
    organization?._id,
  );
  if (!organization || organizationRole === null) {
    return { refusal: NO_ORGANIZATION };
  }
  return { organization, role: effectiveRole(organization, organizationRole) };
}

async function userOf(ctx: Ctx, identity: UserIdentity) {
  return await ctx.db
    .query("users")
    .withIndex("by_clerk_id", (q) => q.eq("clerkId", identity.subject))
    .unique();
}

/**
 * Get current user from database - throws if not found, or if the call
 * carries no organisation the server accepts.
 */
export async function getCurrentUserOrThrow(ctx: Ctx): Promise<CurrentUser> {
  const identity = await requireAuth(ctx);
  const user = await userOf(ctx, identity);
  if (!user) {
    throw new Error(
      "User not found in database. Please complete registration.",
    );
  }
  const session = await sessionOf(ctx, identity, user);
  if ("refusal" in session) throw new ConvexError(session.refusal);
  return {
    ...user,
    role: session.role,
    organizationId: session.organization._id,
  };
}

/**
 * The caller's account, and the organisation and role of the call when the
 * server accepts them (null otherwise). Returns null when signed out or not
 * registered. Never throws: this is what the site reads to decide what to show.
 */
export async function getCurrentUser(
  ctx: Ctx,
): Promise<{ user: Doc<"users">; session: Session | null } | null> {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) {
    return null;
  }
  const user = await userOf(ctx, identity);
  if (!user) return null;
  const session = await sessionOf(ctx, identity, user);
  return { user, session: "refusal" in session ? null : session };
}

/**
 * Require specific role(s) - throws if not authorized
 */
export async function requireRole(ctx: Ctx, allowedRoles: Role[]) {
  const user = await getCurrentUserOrThrow(ctx);

  if (!allowedRoles.includes(user.role)) {
    throw new Error(
      `Unauthorized. Required roles: ${allowedRoles.join(", ")}. Your role: ${user.role}`,
    );
  }

  return user;
}

/** True when the row belongs to the caller's own organisation. */
export function sameOrganization(
  caller: CurrentUser,
  organizationId: Id<"organizations"> | undefined,
): boolean {
  return (
    organizationId !== undefined && organizationId === caller.organizationId
  );
}

/**
 * True when the caller may be served a row of this organisation at all: the
 * Anheart admin always, anyone else for their own organisation only. A row
 * with no organisation is in nobody's scope but the Anheart admin's.
 */
export function inScope(
  caller: CurrentUser,
  organizationId: Id<"organizations"> | undefined,
): boolean {
  return caller.role === "admin" || sameOrganization(caller, organizationId);
}

/**
 * Check if a gestionnaire manages a specific user in an organisation (via
 * user_gestionnaires relation)
 */
export async function isGestionnaireOfUser(
  ctx: Ctx,
  gestionnaireId: Id<"users">,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
): Promise<boolean> {
  const relations = await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_user_and_gestionnaire", (q) =>
      q.eq("userId", userId).eq("gestionnaireId", gestionnaireId),
    )
    .collect();

  return relations.some((r) => r.organizationId === organizationId);
}

/**
 * Check if a gestionnaire manages a specific machine (via machine_gestionnaires relation)
 */
export async function isGestionnaireOfMachine(
  ctx: Ctx,
  gestionnaireId: Id<"users">,
  machineId: Id<"machines">,
): Promise<boolean> {
  const relation = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_machine_and_gestionnaire", (q) =>
      q.eq("machineId", machineId).eq("gestionnaireId", gestionnaireId),
    )
    .unique();

  return relation !== null;
}

/**
 * Check if the caller manages the target user: the Anheart admin; an
 * organisation admin for an active member of their organisation; a
 * gestionnaire for an active member they are linked to in their organisation.
 */
export async function canManageUser(
  ctx: Ctx,
  targetUserId: Id<"users">,
  currentUser: CurrentUser,
): Promise<boolean> {
  if (currentUser.role === "admin") {
    return true;
  }
  if (currentUser.role === "user") {
    return false;
  }

  // Same organisation: the target is an active member of the caller's
  const targetRole = await organizationRoleOf(
    ctx,
    targetUserId,
    currentUser.organizationId,
  );
  if (targetRole === null) {
    return false;
  }
  if (currentUser.role === "org_admin") {
    return true;
  }

  return await isGestionnaireOfUser(
    ctx,
    currentUser._id,
    targetUserId,
    currentUser.organizationId,
  );
}

/**
 * Check if current user can access target user's data: themselves, or a user
 * they manage (`canManageUser`)
 */
export async function canAccessUser(
  ctx: Ctx,
  targetUserId: Id<"users">,
  currentUser: CurrentUser,
): Promise<boolean> {
  // Users can access themselves
  if (currentUser._id === targetUserId) {
    return true;
  }

  return await canManageUser(ctx, targetUserId, currentUser);
}

/**
 * Check if current user can access a machine: the Anheart admin; in the
 * machine's organisation, an organisation admin or a gestionnaire linked to
 * the machine (via machine_gestionnaires relation)
 */
export async function canAccessMachine(
  ctx: Ctx,
  machineId: Id<"machines">,
  currentUser: CurrentUser,
): Promise<boolean> {
  // Anheart admin can access any machine
  if (currentUser.role === "admin") {
    return true;
  }
  if (currentUser.role === "user") {
    return false;
  }

  const machine = await ctx.db.get(machineId);
  if (!machine || !sameOrganization(currentUser, machine.organizationId)) {
    return false;
  }
  if (currentUser.role === "org_admin") {
    return true;
  }

  return await isGestionnaireOfMachine(ctx, currentUser._id, machineId);
}

/**
 * Check if current user can manage (edit/delete) a machine
 */
export async function canManageMachine(
  ctx: Ctx,
  machineId: Id<"machines">,
  currentUser: CurrentUser,
): Promise<boolean> {
  return await canAccessMachine(ctx, machineId, currentUser);
}

/**
 * Check if current user can read a session: the Anheart admin; in the
 * session's organisation, its rider or whoever can access its machine.
 */
export async function canAccessSession(
  ctx: Ctx,
  session: Pick<Doc<"sessions">, "organizationId" | "userId" | "machineId">,
  currentUser: CurrentUser,
): Promise<boolean> {
  if (currentUser.role === "admin") {
    return true;
  }
  if (!sameOrganization(currentUser, session.organizationId)) {
    return false;
  }
  if (session.userId === currentUser._id) {
    return true;
  }

  return await canAccessMachine(ctx, session.machineId, currentUser);
}

/**
 * Require the right to administer a gestionnaire, that is to decide which
 * machines and which patients they manage. Returns the caller, the target
 * gestionnaire and the organisation the decision applies to.
 *
 * The Anheart admin administers any gestionnaire, in the gestionnaire's main
 * organisation. An organisation admin administers the gestionnaires of their
 * own organisation, in that organisation: an account that is not an active
 * member of it answers like an account that does not exist.
 */
export async function requireGestionnaireAdmin(
  ctx: Ctx,
  gestionnaireId: Id<"users">,
) {
  // The role is checked first: a caller who is not allowed learns nothing
  // about the target account.
  const currentUser = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);

  const gestionnaire = await ctx.db.get(gestionnaireId);
  const organizationId =
    currentUser.role === "admin"
      ? gestionnaire?.organizationId
      : currentUser.organizationId;
  // The role held in that organisation, never the stored mirror.
  const role = await organizationRoleOf(ctx, gestionnaireId, organizationId);
  if (!gestionnaire || (currentUser.role !== "admin" && role === null)) {
    throw new Error("Gestionnaire not found");
  }
  if (organizationId === undefined || role !== "gestionnaire") {
    throw new Error("Target user is not a gestionnaire");
  }

  return { currentUser, gestionnaire, organizationId };
}
