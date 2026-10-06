import {
  query,
  mutation,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import { ConvexError, v } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import { effectiveHrMax } from "./training";
import {
  ORGANIZATION_ADMIN_ROLES,
  STAFF_ROLES,
  anheartClerkOrganizationId,
  requireAuth,
  requireRole,
  getCurrentUserOrThrow,
  getCurrentUser as getUserFromCtx,
  canAccessUser,
  canManageUser,
  findMembership,
  findUnlinkedDefaultOrganization,
  inScope,
  organizationFromClaims,
  organizationRoleOf,
  type CurrentUser,
  type OrganizationRole,
} from "./lib/auth";

/**
 * Write the mirror of a membership the verified identity carries: the
 * membership row, the mirrored role, and the main organisation of an account
 * that has none yet.
 */
async function mirrorMembership(
  ctx: MutationCtx,
  user: Doc<"users">,
  organizationId: Id<"organizations">,
  role: OrganizationRole,
) {
  const membership = await findMembership(ctx, user._id, organizationId);
  if (membership) {
    await ctx.db.patch(membership._id, { role, active: true });
  } else {
    await ctx.db.insert("memberships", {
      userId: user._id,
      organizationId,
      role,
      active: true,
    });
  }
  await ctx.db.patch(user._id, {
    role,
    organizationId: user.organizationId ?? organizationId,
  });
}

/**
 * Sync Clerk user to Convex database on first login
 *
 * The account joins the organisation its verified token names, with the role
 * the token carries. Without an organisation claim, and only before Clerk
 * Organizations is configured for the deployment, it joins the default
 * organisation as a plain user. Otherwise it is created without organisation.
 */
export const getOrCreateUser = mutation({
  args: {},
  returns: v.id("users"),
  handler: async (ctx) => {
    const identity = await requireAuth(ctx);

    // Check if user already exists
    const existing = await ctx.db
      .query("users")
      .withIndex("by_clerk_id", (q) => q.eq("clerkId", identity.subject))
      .unique();
    const claimed = await organizationFromClaims(ctx, identity);

    if (existing) {
      if (claimed.kind === "claimed") {
        await mirrorMembership(
          ctx,
          existing,
          claimed.organization._id,
          claimed.organizationRole,
        );
      }
      return existing._id;
    }

    let home: {
      organizationId: Id<"organizations">;
      role: OrganizationRole;
    } | null = null;
    if (claimed.kind === "claimed") {
      home = {
        organizationId: claimed.organization._id,
        role: claimed.organizationRole,
      };
    } else if (claimed.kind === "unclaimed") {
      const organization = await findUnlinkedDefaultOrganization(ctx);
      if (organization)
        home = { organizationId: organization._id, role: "user" };
    }

    // Create new user with default role
    const userId = await ctx.db.insert("users", {
      clerkId: identity.subject,
      role: home?.role ?? "user",
      organizationId: home?.organizationId,
      firstName: identity.givenName ?? "",
      lastName: identity.familyName ?? "",
      email: identity.email ?? "",
      language: "fr",
      createdAt: Date.now(),
    });
    if (home) {
      await ctx.db.insert("memberships", {
        userId,
        organizationId: home.organizationId,
        role: home.role,
        active: true,
      });
    }

    return userId;
  },
});

/**
 * Get current authenticated user
 *
 * `role` and `organization` are those of the call, read from the verified
 * identity. When the server accepts no organisation for the call (no active
 * organisation, unknown organisation or role), `organization` is null and
 * `role` is "user": every organisation-scoped function then refuses.
 */
export const getCurrentUser = query({
  args: {},
  returns: v.union(
    v.object({
      _id: v.id("users"),
      _creationTime: v.number(),
      clerkId: v.string(),
      role: v.union(
        v.literal("admin"),
        v.literal("org_admin"),
        v.literal("gestionnaire"),
        v.literal("user"),
      ),
      organization: v.union(
        v.object({
          _id: v.id("organizations"),
          name: v.string(),
          slug: v.string(),
        }),
        v.null(),
      ),
      gestionnaireId: v.optional(v.id("users")),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      language: v.union(v.literal("fr"), v.literal("en")),
      createdAt: v.number(),
      hrMax: v.optional(v.number()),
      birthYear: v.optional(v.number()),
    }),
    v.null(),
  ),
  handler: async (ctx) => {
    const current = await getUserFromCtx(ctx);
    if (!current) return null;
    const { user, session } = current;
    return {
      _id: user._id,
      _creationTime: user._creationTime,
      clerkId: user.clerkId,
      role: session?.role ?? ("user" as const),
      organization: session
        ? {
            _id: session.organization._id,
            name: session.organization.name,
            slug: session.organization.slug,
          }
        : null,
      gestionnaireId: user.gestionnaireId,
      firstName: user.firstName,
      lastName: user.lastName,
      email: user.email,
      language: user.language,
      createdAt: user.createdAt,
      hrMax: user.hrMax,
      birthYear: user.birthYear,
    };
  },
});

/**
 * Update user role (Anheart admin only, and only until Clerk Organizations
 * carries the roles: from then on a role is a read-only mirror of Clerk)
 */
export const updateUserRole = mutation({
  args: {
    userId: v.id("users"),
    role: v.union(
      v.literal("admin"),
      v.literal("gestionnaire"),
      v.literal("user"),
    ),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin"]);

    if (anheartClerkOrganizationId() !== null) {
      throw new ConvexError("Roles are managed in Clerk Organizations");
    }

    const membership = await findMembership(
      ctx,
      args.userId,
      currentUser.organizationId,
    );
    if (!membership) {
      throw new Error("User not found");
    }

    await ctx.db.patch(membership._id, { role: args.role });
    await ctx.db.patch(args.userId, { role: args.role });
    return null;
  },
});

/**
 * Update own profile
 */
export const updateUserProfile = mutation({
  args: {
    firstName: v.optional(v.string()),
    lastName: v.optional(v.string()),
    language: v.optional(v.union(v.literal("fr"), v.literal("en"))),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    const updates: Record<string, string> = {};
    if (args.firstName !== undefined) updates.firstName = args.firstName;
    if (args.lastName !== undefined) updates.lastName = args.lastName;
    if (args.language !== undefined) updates.language = args.language;

    if (Object.keys(updates).length > 0) {
      await ctx.db.patch(currentUser._id, updates);
    }

    return null;
  },
});

/** The patients a gestionnaire is linked to, in the organisations the caller may see. */
async function patientLinksOfGestionnaire(
  ctx: QueryCtx | MutationCtx,
  caller: CurrentUser,
  gestionnaireId: Id<"users">,
) {
  const relations = await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", gestionnaireId))
    .collect();
  return relations.filter((r) => inScope(caller, r.organizationId));
}

/** What a listing shows of an account. */
type ListedUser = Pick<
  Doc<"users">,
  "_id" | "firstName" | "lastName" | "email" | "language" | "createdAt"
>;

/**
 * List users with role-based filtering
 *
 * Anheart admin: every account. Organisation admin: the active members of
 * their organisation. Gestionnaire: their patients in their organisation.
 * User: themselves. `role` is the role held in the caller's organisation
 * (the stored mirror for the Anheart admin, who lists across organisations).
 */
export const listUsers = query({
  args: {
    gestionnaireId: v.optional(v.id("users")),
    role: v.optional(
      v.union(v.literal("admin"), v.literal("gestionnaire"), v.literal("user")),
    ),
  },
  returns: v.array(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      role: v.string(),
      language: v.string(),
      createdAt: v.number(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    let users: Array<{ user: ListedUser; role: OrganizationRole }> = [];

    /** Active members of the caller's organisation among these accounts. */
    const membersAmong = async (userIds: Id<"users">[]) => {
      const members: typeof users = [];
      for (const userId of new Set(userIds)) {
        const role = await organizationRoleOf(
          ctx,
          userId,
          currentUser.organizationId,
        );
        const user = role === null ? null : await ctx.db.get(userId);
        if (user && role !== null) members.push({ user, role });
      }
      return members;
    };

    if (currentUser.role === "admin") {
      // Anheart admin can see all users, optionally filtered by gestionnaire
      let userDocs: Array<Doc<"users"> | null>;
      if (args.gestionnaireId) {
        // Get users managed by this gestionnaire via relation table
        const patients = await patientLinksOfGestionnaire(
          ctx,
          currentUser,
          args.gestionnaireId,
        );
        const userIds = [...new Set(patients.map((p) => p.userId))];
        userDocs = await Promise.all(userIds.map((id) => ctx.db.get(id)));
      } else {
        userDocs = await ctx.db.query("users").collect();
      }
      users = userDocs
        .filter((u): u is Doc<"users"> => u !== null)
        .map((user) => ({ user, role: user.role }));
    } else if (currentUser.role === "org_admin") {
      // Organisation admin sees the members of their organisation
      if (args.gestionnaireId) {
        const patients = await patientLinksOfGestionnaire(
          ctx,
          currentUser,
          args.gestionnaireId,
        );
        users = await membersAmong(patients.map((p) => p.userId));
      } else {
        const memberships = await ctx.db
          .query("memberships")
          .withIndex("by_organization", (q) =>
            q.eq("organizationId", currentUser.organizationId),
          )
          .collect();
        users = await membersAmong(memberships.map((m) => m.userId));
      }
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees their own patients via relation table
      const patients = await patientLinksOfGestionnaire(
        ctx,
        currentUser,
        currentUser._id,
      );
      users = await membersAmong(patients.map((p) => p.userId));
    } else {
      // Regular users see only themselves
      users = [{ user: currentUser, role: "user" }];
    }

    // Filter by role if specified
    if (args.role) {
      users = users.filter((u) => u.role === args.role);
    }

    return users.map(({ user: u, role }) => ({
      _id: u._id,
      firstName: u.firstName,
      lastName: u.lastName,
      email: u.email,
      role,
      language: u.language,
      createdAt: u.createdAt,
    }));
  },
});

/**
 * Create a patient (gestionnaire or admin), in the caller's organisation
 */
export const createPatient = mutation({
  args: {
    firstName: v.string(),
    lastName: v.string(),
    email: v.string(),
    language: v.optional(v.union(v.literal("fr"), v.literal("en"))),
    gestionnaireIds: v.optional(v.array(v.id("users"))), // Admin can assign multiple gestionnaires
  },
  returns: v.id("users"),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, STAFF_ROLES);
    const organizationId = currentUser.organizationId;

    // Validate email format (basic check)
    if (!args.email.includes("@")) {
      throw new Error("Invalid email format");
    }

    // Check email not already in use
    const existingUsers = await ctx.db.query("users").collect();
    const conflictingUser = existingUsers.find(
      (u) => u.email.toLowerCase() === args.email.toLowerCase(),
    );
    if (conflictingUser) {
      // The account is named only to a caller of its own organisation
      const conflictingRole =
        currentUser.role === "admin"
          ? conflictingUser.role
          : await organizationRoleOf(ctx, conflictingUser._id, organizationId);
      if (conflictingRole === null) {
        throw new Error("Email already in use");
      }
      throw new Error(
        `Email already in use by an existing ${conflictingRole} account (${conflictingUser.firstName} ${conflictingUser.lastName})`,
      );
    }

    const now = Date.now();

    // Create patient (no gestionnaireId on user - using relation table instead)
    const userId = await ctx.db.insert("users", {
      clerkId: "", // Will be set when patient accepts Clerk invitation
      role: "user",
      organizationId,
      firstName: args.firstName,
      lastName: args.lastName,
      email: args.email,
      language: args.language ?? "fr",
      createdAt: now,
    });
    await ctx.db.insert("memberships", {
      userId,
      organizationId,
      role: "user",
      active: true,
    });

    // Create user-gestionnaire relations
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire creating patient - auto-assign themselves
      await ctx.db.insert("user_gestionnaires", {
        organizationId,
        userId,
        gestionnaireId: currentUser._id,
        createdAt: now,
        createdBy: currentUser._id,
      });
    } else if (args.gestionnaireIds) {
      // Admin can assign multiple gestionnaires
      for (const gestionnaireId of args.gestionnaireIds) {
        // Verify the gestionnaire has the gestionnaire role in this organisation
        const role = await organizationRoleOf(
          ctx,
          gestionnaireId,
          organizationId,
        );
        if (role === "gestionnaire") {
          await ctx.db.insert("user_gestionnaires", {
            organizationId,
            userId,
            gestionnaireId,
            createdAt: now,
            createdBy: currentUser._id,
          });
        }
      }
    }

    return userId;
  },
});

/**
 * Update a patient (admin or gestionnaire can edit their patients)
 */
export const updatePatient = mutation({
  args: {
    userId: v.id("users"),
    firstName: v.optional(v.string()),
    lastName: v.optional(v.string()),
    language: v.optional(v.union(v.literal("fr"), v.literal("en"))),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check access rights
    if (!(await canManageUser(ctx, args.userId, currentUser))) {
      throw new Error("Not authorized to edit this patient");
    }

    // Check if user exists
    const targetUser = await ctx.db.get(args.userId);
    if (!targetUser) {
      throw new Error("User not found");
    }

    const updates: Record<string, string> = {};
    if (args.firstName !== undefined) updates.firstName = args.firstName;
    if (args.lastName !== undefined) updates.lastName = args.lastName;
    if (args.language !== undefined) updates.language = args.language;

    if (Object.keys(updates).length > 0) {
      await ctx.db.patch(args.userId, updates);
    }

    return null;
  },
});

/**
 * Get a specific user by ID
 */
export const getUserById = query({
  args: {
    userId: v.id("users"),
  },
  returns: v.union(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      role: v.string(),
      language: v.string(),
      gestionnaireId: v.optional(v.id("users")),
      createdAt: v.number(),
      hrMax: v.optional(v.number()),
      birthYear: v.optional(v.number()),
      // Measured maximum, else the Tanaka estimate; null when neither is set.
      effectiveHrMax: v.union(v.number(), v.null()),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const hasAccess = await canAccessUser(ctx, args.userId, currentUser);
    if (!hasAccess) {
      return null;
    }

    const user = await ctx.db.get(args.userId);
    if (!user) return null;

    // The role held in the caller's organisation; the stored mirror for the
    // Anheart admin, who reads across organisations.
    const role =
      (currentUser.role === "admin"
        ? null
        : await organizationRoleOf(
            ctx,
            user._id,
            currentUser.organizationId,
          )) ?? user.role;

    return {
      _id: user._id,
      firstName: user.firstName,
      lastName: user.lastName,
      email: user.email,
      role,
      language: user.language,
      gestionnaireId: user.gestionnaireId,
      createdAt: user.createdAt,
      hrMax: user.hrMax,
      birthYear: user.birthYear,
      effectiveHrMax: effectiveHrMax(user, Date.now()),
    };
  },
});

/**
 * Delete a user
 *
 * The Anheart admin deletes the account everywhere. An organisation admin or a
 * gestionnaire removes it from THEIR organisation only (membership, links and
 * launch rights of that organisation); the account itself is deleted only when
 * no other organisation holds it.
 */
export const deleteUser = mutation({
  args: {
    userId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Cannot delete self
    if (currentUser._id === args.userId) {
      throw new Error("Cannot delete your own account");
    }

    // Check permissions
    if (currentUser.role === "user") {
      throw new Error("Not authorized to delete users");
    }
    if (!(await canManageUser(ctx, args.userId, currentUser))) {
      throw new Error("Not authorized to delete this user");
    }
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire can only delete their own patients (via relation)
      const targetRole = await organizationRoleOf(
        ctx,
        args.userId,
        currentUser.organizationId,
      );
      if (targetRole !== "user") {
        throw new Error("Can only delete patient accounts");
      }
    }

    const targetUser = await ctx.db.get(args.userId);
    if (!targetUser) {
      throw new Error("User not found");
    }

    // What the caller may remove: every organisation for the Anheart admin,
    // their own organisation for anyone else.
    const removable = (organizationId: Id<"organizations"> | undefined) =>
      inScope(currentUser, organizationId);

    // Delete user-gestionnaire relations where this user is the patient
    const patientRelations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    // ... and those where this user is the gestionnaire
    const managerRelations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", args.userId))
      .collect();
    // Also delete machine-gestionnaire relations
    const machineRelations = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", args.userId))
      .collect();
    // ... and launch rights
    const launchRights = await ctx.db
      .query("machine_user_permissions")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    for (const row of [
      ...patientRelations,
      ...managerRelations,
      ...machineRelations,
      ...launchRights,
    ]) {
      if (removable(row.organizationId)) await ctx.db.delete(row._id);
    }

    // Memberships: the ones removed, and the ones another organisation keeps
    const memberships = await ctx.db
      .query("memberships")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    const kept = memberships.filter((m) => !removable(m.organizationId));
    for (const membership of memberships) {
      if (removable(membership.organizationId)) {
        await ctx.db.delete(membership._id);
      }
    }

    if (kept.length === 0) {
      // Delete user
      await ctx.db.delete(args.userId);
    } else if (removable(targetUser.organizationId)) {
      // The account stays in another organisation, which becomes its main one
      await ctx.db.patch(args.userId, {
        organizationId: kept[0].organizationId,
        role: kept[0].role,
      });
    }

    return null;
  },
});

/**
 * Lowercase the ASCII letters of an address and nothing else, so that two
 * addresses that differ by a non-ASCII character never compare equal.
 */
function asciiLowerCase(value: string): string {
  return value.replace(/[A-Z]/g, (letter) =>
    String.fromCharCode(letter.charCodeAt(0) + 32),
  );
}

/**
 * Link patient to Clerk account (called when patient accepts invitation)
 *
 * The record is selected by the caller's verified email address, read from the
 * identity (JWT claims `email` and `email_verified`). The `email` argument is
 * not authoritative: it must designate that same address. Addresses match
 * case-insensitively for ASCII letters only. Whenever nothing is linked the
 * result is null, and a record that is already linked is never linked again.
 *
 * When the identity names an organisation, only a record of that organisation
 * is linked; an organisation the server refuses links nothing.
 */
export const linkPatientToClerk = mutation({
  args: {
    email: v.string(),
  },
  returns: v.union(v.id("users"), v.null()),
  handler: async (ctx, args) => {
    const identity = await requireAuth(ctx);

    // Linking requires the caller's verified address
    if (!identity.email || identity.emailVerified !== true) {
      return null;
    }
    const verifiedEmail = asciiLowerCase(identity.email);
    if (asciiLowerCase(args.email) !== verifiedEmail) {
      return null;
    }

    const claimed = await organizationFromClaims(ctx, identity);
    if (claimed.kind === "refused") {
      return null;
    }

    // Find user by verified email with empty clerkId
    const users = await ctx.db.query("users").collect();
    const user = users.find(
      (u) =>
        asciiLowerCase(u.email) === verifiedEmail &&
        u.clerkId === "" &&
        (claimed.kind === "unclaimed" ||
          u.organizationId === claimed.organization._id),
    );

    if (!user) {
      return null;
    }

    // Link the Clerk account
    await ctx.db.patch(user._id, {
      clerkId: identity.subject,
    });

    return user._id;
  },
});

/** The link rows between a patient and a gestionnaire, one per organisation. */
async function linksBetween(
  ctx: MutationCtx,
  userId: Id<"users">,
  gestionnaireId: Id<"users">,
) {
  return await ctx.db
    .query("user_gestionnaires")
    .withIndex("by_user_and_gestionnaire", (q) =>
      q.eq("userId", userId).eq("gestionnaireId", gestionnaireId),
    )
    .collect();
}

/**
 * Assign a gestionnaire to a patient (admin only, or gestionnaire to their own patients)
 *
 * The link lives in the caller's organisation (for the Anheart admin, in the
 * patient's main organisation); the patient and the gestionnaire must both be
 * active members of it, with those roles.
 */
export const assignGestionnaireToUser = mutation({
  args: {
    userId: v.id("users"),
    gestionnaireId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    const organizationId =
      currentUser.role === "admin"
        ? (await ctx.db.get(args.userId))?.organizationId
        : currentUser.organizationId;

    // Verify target user is a patient of the organisation
    const targetRole = await organizationRoleOf(
      ctx,
      args.userId,
      organizationId,
    );
    if (organizationId === undefined || targetRole !== "user") {
      throw new Error("Can only assign gestionnaires to patients");
    }

    // Verify gestionnaire has the correct role in the organisation
    const gestionnaireRole = await organizationRoleOf(
      ctx,
      args.gestionnaireId,
      organizationId,
    );
    if (gestionnaireRole !== "gestionnaire") {
      throw new Error("Target user is not a gestionnaire");
    }

    // Check permissions
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire can only add themselves
      if (args.gestionnaireId !== currentUser._id) {
        throw new Error("Gestionnaires can only assign themselves to patients");
      }
    }

    // Check if relation already exists
    const existingRelations = await linksBetween(
      ctx,
      args.userId,
      args.gestionnaireId,
    );
    if (existingRelations.some((r) => r.organizationId === organizationId)) {
      throw new Error("Gestionnaire already assigned to this patient");
    }

    // Create relation
    await ctx.db.insert("user_gestionnaires", {
      organizationId,
      userId: args.userId,
      gestionnaireId: args.gestionnaireId,
      createdAt: Date.now(),
      createdBy: currentUser._id,
    });

    return null;
  },
});

/**
 * Remove a gestionnaire from a patient
 */
export const removeGestionnaireFromUser = mutation({
  args: {
    userId: v.id("users"),
    gestionnaireId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, STAFF_ROLES);

    // Check permissions
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire can only remove themselves
      if (args.gestionnaireId !== currentUser._id) {
        throw new Error(
          "Gestionnaires can only remove themselves from patients",
        );
      }
    }

    // Find and delete the relation, in the organisations the caller may act in
    const relations = (
      await linksBetween(ctx, args.userId, args.gestionnaireId)
    ).filter((r) => inScope(currentUser, r.organizationId));

    if (relations.length === 0) {
      throw new Error("Gestionnaire is not assigned to this patient");
    }

    for (const relation of relations) {
      await ctx.db.delete(relation._id);
    }

    return null;
  },
});

/**
 * Get all gestionnaires for a patient
 */
export const getGestionnairesForPatient = query({
  args: {
    userId: v.id("users"),
  },
  returns: v.array(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const hasAccess = await canAccessUser(ctx, args.userId, currentUser);
    if (!hasAccess) {
      return [];
    }

    const relations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    const gestionnaireIds = new Set(
      relations
        .filter((r) => inScope(currentUser, r.organizationId))
        .map((r) => r.gestionnaireId),
    );

    const gestionnaires = await Promise.all(
      [...gestionnaireIds].map((id) => ctx.db.get(id)),
    );

    return gestionnaires
      .filter((g) => g !== null)
      .map((g) => ({
        _id: g!._id,
        firstName: g!.firstName,
        lastName: g!.lastName,
        email: g!.email,
      }));
  },
});

/**
 * List all gestionnaires (admin only): every organisation for the Anheart
 * admin, their own organisation for an organisation admin
 */
export const listGestionnaires = query({
  args: {},
  returns: v.array(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      createdAt: v.number(),
      machineCount: v.number(),
      patientCount: v.number(),
    }),
  ),
  handler: async (ctx) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    if (!ORGANIZATION_ADMIN_ROLES.includes(currentUser.role)) {
      return [];
    }

    // Get all active members with role gestionnaire
    const memberships =
      currentUser.role === "admin"
        ? await ctx.db.query("memberships").collect()
        : await ctx.db
            .query("memberships")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", currentUser.organizationId),
            )
            .collect();
    const gestionnaireIds = new Set(
      memberships
        .filter((m) => m.active && m.role === "gestionnaire")
        .map((m) => m.userId),
    );

    // Get machine and patient counts for each
    const result = [];
    for (const gestionnaireId of gestionnaireIds) {
      const g = await ctx.db.get(gestionnaireId);
      if (!g) continue;

      const machineRelations = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", g._id))
        .collect();

      const patientRelations = await ctx.db
        .query("user_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", g._id))
        .collect();

      result.push({
        _id: g._id,
        firstName: g.firstName,
        lastName: g.lastName,
        email: g.email,
        createdAt: g.createdAt,
        machineCount: machineRelations.filter((r) =>
          inScope(currentUser, r.organizationId),
        ).length,
        patientCount: patientRelations.filter((r) =>
          inScope(currentUser, r.organizationId),
        ).length,
      });
    }

    return result;
  },
});

/**
 * Assign patients to a gestionnaire (admin only) - replaces all existing
 * assignments of the organisation (the caller's; for the Anheart admin, the
 * gestionnaire's main organisation)
 */
export const assignPatientsToGestionnaire = mutation({
  args: {
    gestionnaireId: v.id("users"),
    patientIds: v.array(v.id("users")),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ORGANIZATION_ADMIN_ROLES);

    const organizationId =
      currentUser.role === "admin"
        ? (await ctx.db.get(args.gestionnaireId))?.organizationId
        : currentUser.organizationId;

    // Verify gestionnaire has the correct role in the organisation
    const gestionnaireRole = await organizationRoleOf(
      ctx,
      args.gestionnaireId,
      organizationId,
    );
    if (organizationId === undefined || gestionnaireRole !== "gestionnaire") {
      throw new Error("Target user is not a gestionnaire");
    }

    const now = Date.now();

    // Remove existing relations for this gestionnaire in the organisation
    const existingRelations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_gestionnaire", (q) =>
        q.eq("gestionnaireId", args.gestionnaireId),
      )
      .collect();

    for (const relation of existingRelations) {
      if (relation.organizationId === organizationId) {
        await ctx.db.delete(relation._id);
      }
    }

    // Add new relations
    for (const patientId of new Set(args.patientIds)) {
      const role = await organizationRoleOf(ctx, patientId, organizationId);
      if (role === "user") {
        await ctx.db.insert("user_gestionnaires", {
          organizationId,
          userId: patientId,
          gestionnaireId: args.gestionnaireId,
          createdAt: now,
          createdBy: currentUser._id,
        });
      }
    }

    return null;
  },
});

/**
 * Get all patients for a gestionnaire
 */
export const getPatientsForGestionnaire = query({
  args: {
    gestionnaireId: v.optional(v.id("users")), // If not provided, uses current user
  },
  returns: v.array(
    v.object({
      _id: v.id("users"),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      language: v.string(),
      createdAt: v.number(),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Determine which gestionnaire to query for
    let targetGestionnaireId = args.gestionnaireId;

    // Admin can query any gestionnaire, gestionnaires only their own
    if (currentUser.role === "gestionnaire") {
      targetGestionnaireId ??= currentUser._id;
      if (targetGestionnaireId !== currentUser._id) {
        return [];
      }
    } else if (!ORGANIZATION_ADMIN_ROLES.includes(currentUser.role)) {
      return [];
    }

    if (!targetGestionnaireId) {
      // Admin didn't specify a gestionnaire - return empty
      return [];
    }

    const relations = await patientLinksOfGestionnaire(
      ctx,
      currentUser,
      targetGestionnaireId,
    );

    const patients = [];
    const seen = new Set<Id<"users">>();
    for (const relation of relations) {
      if (seen.has(relation.userId)) continue;
      seen.add(relation.userId);
      // Still a patient of the organisation the link lives in
      const role = await organizationRoleOf(
        ctx,
        relation.userId,
        relation.organizationId,
      );
      const patient =
        role === "user" ? await ctx.db.get(relation.userId) : null;
      if (patient) patients.push(patient);
    }

    return patients.map((p) => ({
      _id: p._id,
      firstName: p.firstName,
      lastName: p.lastName,
      email: p.email,
      language: p.language,
      createdAt: p.createdAt,
    }));
  },
});
