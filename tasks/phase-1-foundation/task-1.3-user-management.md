# Task 1.3: User Management

## Objective

Implement user CRUD operations with proper role-based access control, including Clerk synchronization and patient creation by gestionnaires.

## File to Create

`convex/users.ts`

## Dependencies

- Task 1.1 (Database Schema) must be completed
- Task 1.2 (Auth Helpers) must be completed

---

## Acceptance Criteria

### getOrCreateUser Mutation

- [ ] Function `getOrCreateUser` exported as mutation
- [ ] Has empty args `{}`
- [ ] Returns `v.id("users")`
- [ ] If user exists (by clerkId), returns existing user ID
- [ ] If user doesn't exist, creates new user with:
  - clerkId from identity.subject
  - role: "user" (default)
  - firstName from identity.givenName (or empty string)
  - lastName from identity.familyName (or empty string)
  - email from identity.email (or empty string)
  - language: "fr" (default)
  - createdAt: current timestamp

### getCurrentUser Query

- [ ] Function `getCurrentUser` exported as query
- [ ] Has empty args `{}`
- [ ] Returns user object or null
- [ ] Returns null if not authenticated
- [ ] Returns null if user not in database
- [ ] Returns full user document if found

### updateUserRole Mutation

- [ ] Function `updateUserRole` exported as mutation
- [ ] Args: userId (id), role (union of roles)
- [ ] Returns `v.null()`
- [ ] Only admin can call this function
- [ ] Throws if user not found
- [ ] Updates user's role

### updateUserProfile Mutation

- [ ] Function `updateUserProfile` exported as mutation
- [ ] Args: firstName (optional), lastName (optional), language (optional)
- [ ] Returns `v.null()`
- [ ] User can only update their own profile
- [ ] Updates provided fields only

### listUsers Query

- [ ] Function `listUsers` exported as query
- [ ] Args: gestionnaireId (optional), role (optional)
- [ ] Returns array of user objects (without sensitive data)
- [ ] Admin sees all users
- [ ] Gestionnaire sees only their patients
- [ ] User sees only themselves
- [ ] Supports filtering by role

### createPatient Mutation

- [ ] Function `createPatient` exported as mutation
- [ ] Args: firstName, lastName, email, language (optional)
- [ ] Returns `v.id("users")`
- [ ] Only admin and gestionnaire can call
- [ ] Validates email not already in use
- [ ] Creates user with role "user"
- [ ] Sets gestionnaireId to current user (if gestionnaire)
- [ ] Sets empty clerkId (will be set when patient accepts invitation)

### getUser Query

- [ ] Function `getUser` exported as query
- [ ] Args: userId
- [ ] Returns user object or null
- [ ] Respects access control (canAccessUser)

### deleteUser Mutation

- [ ] Function `deleteUser` exported as mutation
- [ ] Args: userId
- [ ] Returns `v.null()`
- [ ] Only admin can delete any user
- [ ] Gestionnaire can delete their own patients
- [ ] Cannot delete self
- [ ] Actually deletes user (or soft delete if preferred)

---

## Implementation

```typescript
// convex/users.ts
import { query, mutation } from "./_generated/server";
import { v } from "convex/values";
import {
  requireAuth,
  requireRole,
  getCurrentUserOrThrow,
  getCurrentUser as getUser,
  canAccessUser,
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
        v.literal("technician"),
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
    return await getUser(ctx);
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
      v.literal("technician"),
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
      v.union(
        v.literal("admin"),
        v.literal("gestionnaire"),
        v.literal("technician"),
        v.literal("user"),
      ),
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

    let users;

    if (currentUser.role === "admin") {
      // Admin can see all users, optionally filtered by gestionnaire
      if (args.gestionnaireId) {
        users = await ctx.db
          .query("users")
          .withIndex("by_gestionnaire", (q) =>
            q.eq("gestionnaireId", args.gestionnaireId),
          )
          .collect();
      } else {
        users = await ctx.db.query("users").collect();
      }
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees their own patients
      users = await ctx.db
        .query("users")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser._id),
        )
        .collect();
    } else if (
      currentUser.role === "technician" &&
      currentUser.gestionnaireId
    ) {
      // Technician sees patients of their gestionnaire
      users = await ctx.db
        .query("users")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser.gestionnaireId),
        )
        .collect();
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

    // Create patient
    const userId = await ctx.db.insert("users", {
      clerkId: "", // Will be set when patient accepts Clerk invitation
      role: "user",
      gestionnaireId:
        currentUser.role === "gestionnaire" ? currentUser._id : undefined,
      firstName: args.firstName,
      lastName: args.lastName,
      email: args.email,
      language: args.language ?? "fr",
      createdAt: Date.now(),
    });

    // TODO: In a future task, trigger Clerk invitation email via action

    return userId;
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
      // Gestionnaire can only delete their own patients
      if (targetUser.gestionnaireId !== currentUser._id) {
        throw new Error("Not authorized to delete this user");
      }
      if (targetUser.role !== "user") {
        throw new Error("Can only delete patient accounts");
      }
    } else {
      throw new Error("Not authorized to delete users");
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
```

---

## Testing Steps

### Test getOrCreateUser

1. Login with a new Clerk account
2. Call `getOrCreateUser` mutation
3. Verify user created in database with correct fields
4. Call again - verify returns same user ID (no duplicate)

### Test updateUserRole

1. Login as admin
2. Call `updateUserRole` for another user
3. Verify role changed
4. Login as non-admin
5. Call `updateUserRole` - verify "Unauthorized" error

### Test listUsers

1. Login as admin - verify sees all users
2. Login as gestionnaire - verify sees only their patients
3. Login as user - verify sees only themselves

### Test createPatient

1. Login as gestionnaire
2. Call `createPatient` with valid data
3. Verify patient created with correct gestionnaireId
4. Try creating with duplicate email - verify error
5. Login as user - verify cannot create patient

### Test deleteUser

1. Login as gestionnaire
2. Delete one of their patients - verify success
3. Try to delete another gestionnaire's patient - verify error
4. Login as admin - verify can delete any user
5. Try to delete self - verify error

---

## Notes

- Email uniqueness is checked against all users (case-insensitive)
- New patients have empty `clerkId` until they accept the invitation
- The `linkPatientToClerk` mutation handles the invitation acceptance flow
- Consider adding soft delete (isDeleted field) instead of hard delete for data retention
