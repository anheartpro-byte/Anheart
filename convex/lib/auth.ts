import { QueryCtx, MutationCtx } from "../_generated/server";
import { Id } from "../_generated/dataModel";

export type Role = "admin" | "gestionnaire" | "technician" | "user";

/**
 * Require authentication - throws if not logged in
 */
export async function requireAuth(ctx: QueryCtx | MutationCtx) {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) {
    throw new Error("Not authenticated");
  }
  return identity;
}

/**
 * Get current user from database - throws if not found
 */
export async function getCurrentUserOrThrow(ctx: QueryCtx | MutationCtx) {
  const identity = await requireAuth(ctx);

  const user = await ctx.db
    .query("users")
    .withIndex("by_clerk_id", (q) => q.eq("clerkId", identity.subject))
    .unique();

  if (!user) {
    throw new Error(
      "User not found in database. Please complete registration.",
    );
  }

  return user;
}

/**
 * Get current user from database - returns null if not found
 */
export async function getCurrentUser(ctx: QueryCtx | MutationCtx) {
  const identity = await ctx.auth.getUserIdentity();
  if (!identity) {
    return null;
  }

  return await ctx.db
    .query("users")
    .withIndex("by_clerk_id", (q) => q.eq("clerkId", identity.subject))
    .unique();
}

/**
 * Require specific role(s) - throws if not authorized
 */
export async function requireRole(
  ctx: QueryCtx | MutationCtx,
  allowedRoles: Role[],
) {
  const user = await getCurrentUserOrThrow(ctx);

  if (!allowedRoles.includes(user.role as Role)) {
    throw new Error(
      `Unauthorized. Required roles: ${allowedRoles.join(", ")}. Your role: ${user.role}`,
    );
  }

  return user;
}

/**
 * Check if a gestionnaire manages a specific user (via user_gestionnaires relation)
 */
export async function isGestionnaireOfUser(
  ctx: QueryCtx | MutationCtx,
  gestionnaireId: Id<"users">,
  userId: Id<"users">,
): Promise<boolean> {
  const relation = await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_user_and_gestionnaire", (q) =>
      q.eq("userId", userId).eq("gestionnaireId", gestionnaireId),
    )
    .unique();

  return relation !== null;
}

/**
 * Check if a gestionnaire manages a specific machine (via machine_gestionnaires relation)
 */
export async function isGestionnaireOfMachine(
  ctx: QueryCtx | MutationCtx,
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
 * Get all gestionnaire IDs for a user
 */
export async function getGestionnairesForUser(
  ctx: QueryCtx | MutationCtx,
  userId: Id<"users">,
): Promise<Id<"users">[]> {
  const relations = await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_user", (q) => q.eq("userId", userId))
    .collect();

  return relations.map((r) => r.gestionnaireId);
}

/**
 * Get all gestionnaire IDs for a machine
 */
export async function getGestionnairesForMachine(
  ctx: QueryCtx | MutationCtx,
  machineId: Id<"machines">,
): Promise<Id<"users">[]> {
  const relations = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .collect();

  return relations.map((r) => r.gestionnaireId);
}

/**
 * Check if current user can access target user's data
 */
export async function canAccessUser(
  ctx: QueryCtx | MutationCtx,
  targetUserId: Id<"users">,
): Promise<boolean> {
  const currentUser = await getCurrentUserOrThrow(ctx);

  // Admin can access anyone
  if (currentUser.role === "admin") {
    return true;
  }

  // Users can access themselves
  if (currentUser._id === targetUserId) {
    return true;
  }

  // Gestionnaire can access their patients (via user_gestionnaires relation)
  if (currentUser.role === "gestionnaire") {
    const isManager = await isGestionnaireOfUser(
      ctx,
      currentUser._id,
      targetUserId,
    );
    if (isManager) {
      return true;
    }
  }

  // Technician can access patients of their gestionnaire
  if (currentUser.role === "technician" && currentUser.gestionnaireId) {
    const isManager = await isGestionnaireOfUser(
      ctx,
      currentUser.gestionnaireId,
      targetUserId,
    );
    if (isManager) {
      return true;
    }
  }

  return false;
}

/**
 * Check if current user can access a machine
 */
export async function canAccessMachine(
  ctx: QueryCtx | MutationCtx,
  machineId: Id<"machines">,
): Promise<boolean> {
  const currentUser = await getCurrentUserOrThrow(ctx);

  // Admin can access any machine
  if (currentUser.role === "admin") {
    return true;
  }

  const machine = await ctx.db.get(machineId);
  if (!machine) {
    return false;
  }

  // Gestionnaire can access machines they manage (via machine_gestionnaires relation)
  if (currentUser.role === "gestionnaire") {
    return await isGestionnaireOfMachine(ctx, currentUser._id, machineId);
  }

  // Technician can access machines managed by their gestionnaire
  if (currentUser.role === "technician" && currentUser.gestionnaireId) {
    return await isGestionnaireOfMachine(
      ctx,
      currentUser.gestionnaireId,
      machineId,
    );
  }

  return false;
}

/**
 * Check if current user can manage (edit/delete) a machine
 */
export async function canManageMachine(
  ctx: QueryCtx | MutationCtx,
  machineId: Id<"machines">,
): Promise<boolean> {
  const currentUser = await getCurrentUserOrThrow(ctx);

  // Admin can manage any machine
  if (currentUser.role === "admin") {
    return true;
  }

  const machine = await ctx.db.get(machineId);
  if (!machine) {
    return false;
  }

  // Gestionnaire can manage machines they are associated with
  if (currentUser.role === "gestionnaire") {
    return await isGestionnaireOfMachine(ctx, currentUser._id, machineId);
  }

  // Technicians cannot manage machines (only use them)
  return false;
}
