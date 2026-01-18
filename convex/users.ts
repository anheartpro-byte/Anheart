import { query, mutation } from "./_generated/server";
import { v } from "convex/values";
import {
  requireAuth,
  requireRole,
  getCurrentUserOrThrow,
  getCurrentUser as getUserFromCtx,
  canAccessUser,
  isGestionnaireOfUser,
} from "./lib/auth";

/**
 * Sync Clerk user to Convex database on first login
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

    if (existing) {
      return existing._id;
    }

    // Create new user with default role
    const userId = await ctx.db.insert("users", {
      clerkId: identity.subject,
      role: "user",
      firstName: identity.givenName ?? "",
      lastName: identity.familyName ?? "",
      email: identity.email ?? "",
      language: "fr",
      createdAt: Date.now(),
    });

    return userId;
  },
});

/**
 * Get current authenticated user
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
        v.literal("gestionnaire"),
        v.literal("user"),
      ),
      gestionnaireId: v.optional(v.id("users")),
      firstName: v.string(),
      lastName: v.string(),
      email: v.string(),
      language: v.union(v.literal("fr"), v.literal("en")),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx) => {
    return await getUserFromCtx(ctx);
  },
});

/**
 * Update user role (admin only)
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
    await requireRole(ctx, ["admin"]);

    const user = await ctx.db.get(args.userId);
    if (!user) {
      throw new Error("User not found");
    }

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

/**
 * List users with role-based filtering
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

    let users: Array<{
      _id: typeof currentUser._id;
      firstName: string;
      lastName: string;
      email: string;
      role: string;
      language: string;
      createdAt: number;
    }> = [];

    if (currentUser.role === "admin") {
      // Admin can see all users, optionally filtered by gestionnaire
      if (args.gestionnaireId) {
        // Get users managed by this gestionnaire via relation table
        const relations = await ctx.db
          .query("user_gestionnaires")
          .withIndex("by_gestionnaire", (q) =>
            q.eq("gestionnaireId", args.gestionnaireId!),
          )
          .collect();

        const userIds = relations.map((r) => r.userId);
        const userDocs = await Promise.all(userIds.map((id) => ctx.db.get(id)));
        users = userDocs.filter((u) => u !== null) as typeof users;
      } else {
        users = await ctx.db.query("users").collect();
      }
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees their own patients via relation table
      const relations = await ctx.db
        .query("user_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser._id),
        )
        .collect();

      const userIds = relations.map((r) => r.userId);
      const userDocs = await Promise.all(userIds.map((id) => ctx.db.get(id)));
      users = userDocs.filter((u) => u !== null) as typeof users;
    } else {
      // Regular users see only themselves
      users = [currentUser];
    }

    // Filter by role if specified
    if (args.role) {
      users = users.filter((u) => u.role === args.role);
    }

    return users.map((u) => ({
      _id: u._id,
      firstName: u.firstName,
      lastName: u.lastName,
      email: u.email,
      role: u.role,
      language: u.language,
      createdAt: u.createdAt,
    }));
  },
});

/**
 * Create a patient (gestionnaire or admin)
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
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Validate email format (basic check)
    if (!args.email.includes("@")) {
      throw new Error("Invalid email format");
    }

    // Check email not already in use
    const existingUsers = await ctx.db.query("users").collect();
    const emailExists = existingUsers.some(
      (u) => u.email.toLowerCase() === args.email.toLowerCase(),
    );
    if (emailExists) {
      throw new Error("Email already in use");
    }

    const now = Date.now();

    // Create patient (no gestionnaireId on user - using relation table instead)
    const userId = await ctx.db.insert("users", {
      clerkId: "", // Will be set when patient accepts Clerk invitation
      role: "user",
      firstName: args.firstName,
      lastName: args.lastName,
      email: args.email,
      language: args.language ?? "fr",
      createdAt: now,
    });

    // Create user-gestionnaire relations
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire creating patient - auto-assign themselves
      await ctx.db.insert("user_gestionnaires", {
        userId,
        gestionnaireId: currentUser._id,
        createdAt: now,
        createdBy: currentUser._id,
      });
    } else if (currentUser.role === "admin" && args.gestionnaireIds) {
      // Admin can assign multiple gestionnaires
      for (const gestionnaireId of args.gestionnaireIds) {
        // Verify the gestionnaire exists and has gestionnaire role
        const gestionnaire = await ctx.db.get(gestionnaireId);
        if (gestionnaire && gestionnaire.role === "gestionnaire") {
          await ctx.db.insert("user_gestionnaires", {
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
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Check if user exists and is a patient
    const targetUser = await ctx.db.get(args.userId);
    if (!targetUser) {
      throw new Error("User not found");
    }

    // Check access rights
    if (currentUser.role === "gestionnaire") {
      const isManager = await isGestionnaireOfUser(
        ctx,
        currentUser._id,
        args.userId,
      );
      if (!isManager) {
        throw new Error("Not authorized to edit this patient");
      }
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
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const hasAccess = await canAccessUser(ctx, args.userId);
    if (!hasAccess) {
      return null;
    }

    const user = await ctx.db.get(args.userId);
    if (!user) return null;

    return {
      _id: user._id,
      firstName: user.firstName,
      lastName: user.lastName,
      email: user.email,
      role: user.role,
      language: user.language,
      gestionnaireId: user.gestionnaireId,
      createdAt: user.createdAt,
    };
  },
});

/**
 * Delete a user
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

    const targetUser = await ctx.db.get(args.userId);
    if (!targetUser) {
      throw new Error("User not found");
    }

    // Check permissions
    if (currentUser.role === "admin") {
      // Admin can delete anyone
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire can only delete their own patients (via relation)
      const isManager = await isGestionnaireOfUser(
        ctx,
        currentUser._id,
        args.userId,
      );
      if (!isManager) {
        throw new Error("Not authorized to delete this user");
      }
      if (targetUser.role !== "user") {
        throw new Error("Can only delete patient accounts");
      }
    } else {
      throw new Error("Not authorized to delete users");
    }

    // Delete user-gestionnaire relations where this user is the patient
    const patientRelations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();
    for (const relation of patientRelations) {
      await ctx.db.delete(relation._id);
    }

    // If this user is a gestionnaire, delete their management relations
    if (targetUser.role === "gestionnaire") {
      const managerRelations = await ctx.db
        .query("user_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", args.userId),
        )
        .collect();
      for (const relation of managerRelations) {
        await ctx.db.delete(relation._id);
      }

      // Also delete machine-gestionnaire relations
      const machineRelations = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", args.userId),
        )
        .collect();
      for (const relation of machineRelations) {
        await ctx.db.delete(relation._id);
      }
    }

    // Delete user
    await ctx.db.delete(args.userId);

    return null;
  },
});

/**
 * Link patient to Clerk account (called when patient accepts invitation)
 */
export const linkPatientToClerk = mutation({
  args: {
    email: v.string(),
  },
  returns: v.union(v.id("users"), v.null()),
  handler: async (ctx, args) => {
    const identity = await requireAuth(ctx);

    // Find user by email with empty clerkId
    const users = await ctx.db.query("users").collect();
    const user = users.find(
      (u) =>
        u.email.toLowerCase() === args.email.toLowerCase() && u.clerkId === "",
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

/**
 * Assign a gestionnaire to a patient (admin only, or gestionnaire to their own patients)
 */
export const assignGestionnaireToUser = mutation({
  args: {
    userId: v.id("users"),
    gestionnaireId: v.id("users"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Verify target user exists and is a patient
    const targetUser = await ctx.db.get(args.userId);
    if (!targetUser) {
      throw new Error("User not found");
    }
    if (targetUser.role !== "user") {
      throw new Error("Can only assign gestionnaires to patients");
    }

    // Verify gestionnaire exists and has correct role
    const gestionnaire = await ctx.db.get(args.gestionnaireId);
    if (!gestionnaire) {
      throw new Error("Gestionnaire not found");
    }
    if (gestionnaire.role !== "gestionnaire") {
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
    const existingRelation = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user_and_gestionnaire", (q) =>
        q.eq("userId", args.userId).eq("gestionnaireId", args.gestionnaireId),
      )
      .unique();

    if (existingRelation) {
      throw new Error("Gestionnaire already assigned to this patient");
    }

    // Create relation
    await ctx.db.insert("user_gestionnaires", {
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
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    // Check permissions
    if (currentUser.role === "gestionnaire") {
      // Gestionnaire can only remove themselves
      if (args.gestionnaireId !== currentUser._id) {
        throw new Error(
          "Gestionnaires can only remove themselves from patients",
        );
      }
    }

    // Find and delete the relation
    const relation = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user_and_gestionnaire", (q) =>
        q.eq("userId", args.userId).eq("gestionnaireId", args.gestionnaireId),
      )
      .unique();

    if (!relation) {
      throw new Error("Gestionnaire is not assigned to this patient");
    }

    await ctx.db.delete(relation._id);

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
    const hasAccess = await canAccessUser(ctx, args.userId);
    if (!hasAccess) {
      return [];
    }

    const relations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_user", (q) => q.eq("userId", args.userId))
      .collect();

    const gestionnaires = await Promise.all(
      relations.map((r) => ctx.db.get(r.gestionnaireId)),
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
 * List all gestionnaires (admin only)
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

    if (currentUser.role !== "admin") {
      return [];
    }

    // Get all users with role gestionnaire
    const gestionnaires = await ctx.db
      .query("users")
      .withIndex("by_role", (q) => q.eq("role", "gestionnaire"))
      .collect();

    // Get machine and patient counts for each
    const result = await Promise.all(
      gestionnaires.map(async (g) => {
        const machineRelations = await ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", g._id))
          .collect();

        const patientRelations = await ctx.db
          .query("user_gestionnaires")
          .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", g._id))
          .collect();

        return {
          _id: g._id,
          firstName: g.firstName,
          lastName: g.lastName,
          email: g.email,
          createdAt: g.createdAt,
          machineCount: machineRelations.length,
          patientCount: patientRelations.length,
        };
      }),
    );

    return result;
  },
});

/**
 * Assign patients to a gestionnaire (admin only) - replaces all existing assignments
 */
export const assignPatientsToGestionnaire = mutation({
  args: {
    gestionnaireId: v.id("users"),
    patientIds: v.array(v.id("users")),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin"]);

    // Verify gestionnaire exists and has correct role
    const gestionnaire = await ctx.db.get(args.gestionnaireId);
    if (!gestionnaire) {
      throw new Error("Gestionnaire not found");
    }
    if (gestionnaire.role !== "gestionnaire") {
      throw new Error("Target user is not a gestionnaire");
    }

    const now = Date.now();

    // Remove existing relations for this gestionnaire
    const existingRelations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_gestionnaire", (q) =>
        q.eq("gestionnaireId", args.gestionnaireId),
      )
      .collect();

    for (const relation of existingRelations) {
      await ctx.db.delete(relation._id);
    }

    // Add new relations
    for (const patientId of args.patientIds) {
      const patient = await ctx.db.get(patientId);
      if (patient && patient.role === "user") {
        await ctx.db.insert("user_gestionnaires", {
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

    if (!targetGestionnaireId) {
      if (currentUser.role === "gestionnaire") {
        targetGestionnaireId = currentUser._id;
      } else if (currentUser.role !== "admin") {
        return [];
      }
    }

    // Admin can query any gestionnaire, gestionnaires only their own
    if (currentUser.role !== "admin") {
      if (currentUser.role === "gestionnaire") {
        if (targetGestionnaireId !== currentUser._id) {
          return [];
        }
      } else {
        return [];
      }
    }

    if (!targetGestionnaireId) {
      // Admin didn't specify a gestionnaire - return empty
      return [];
    }

    const relations = await ctx.db
      .query("user_gestionnaires")
      .withIndex("by_gestionnaire", (q) =>
        q.eq("gestionnaireId", targetGestionnaireId!),
      )
      .collect();

    const patients = await Promise.all(
      relations.map((r) => ctx.db.get(r.userId)),
    );

    return patients
      .filter((p) => p !== null && p.role === "user")
      .map((p) => ({
        _id: p!._id,
        firstName: p!.firstName,
        lastName: p!.lastName,
        email: p!.email,
        language: p!.language,
        createdAt: p!.createdAt,
      }));
  },
});
