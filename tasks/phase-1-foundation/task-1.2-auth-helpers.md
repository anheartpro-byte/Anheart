# Task 1.2: Authentication Helpers

## Objective

Create reusable authentication utility functions for role-based access control across all Convex functions.

## File to Create

`convex/lib/auth.ts`

## Dependencies

- Task 1.1 (Database Schema) must be completed

---

## Acceptance Criteria

### Type Definitions

- [ ] `Role` type exported with values: "admin", "gestionnaire", "technician", "user"
- [ ] Type is used consistently across all auth functions

### requireAuth Function

- [ ] Function `requireAuth` exported
- [ ] Accepts `ctx` (QueryCtx or MutationCtx)
- [ ] Returns Clerk identity if authenticated
- [ ] Throws "Not authenticated" error if no identity

### getCurrentUserOrThrow Function

- [ ] Function `getCurrentUserOrThrow` exported
- [ ] Calls `requireAuth` internally
- [ ] Queries users table by clerkId
- [ ] Returns full user document if found
- [ ] Throws "User not found in database" if user doesn't exist

### requireRole Function

- [ ] Function `requireRole` exported
- [ ] Accepts `ctx` and `allowedRoles` array
- [ ] Calls `getCurrentUserOrThrow` internally
- [ ] Returns user if role is in allowedRoles
- [ ] Throws "Unauthorized" error with required roles listed if not authorized

### canAccessUser Function

- [ ] Function `canAccessUser` exported
- [ ] Accepts `ctx` and `targetUserId`
- [ ] Returns `true` if current user is admin
- [ ] Returns `true` if current user is the target user
- [ ] Returns `true` if current user is gestionnaire and target is their patient
- [ ] Returns `false` otherwise

### canAccessMachine Function

- [ ] Function `canAccessMachine` exported
- [ ] Accepts `ctx` and `machineId`
- [ ] Returns `true` if current user is admin
- [ ] Returns `true` if current user is the machine's gestionnaire
- [ ] Returns `true` if current user is technician (for their gestionnaire's machines)
- [ ] Returns `false` otherwise

---

## Implementation

```typescript
// convex/lib/auth.ts
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
    throw new Error("User not found in database");
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

  // Gestionnaire can access their patients
  if (currentUser.role === "gestionnaire") {
    const targetUser = await ctx.db.get(targetUserId);
    if (targetUser?.gestionnaireId === currentUser._id) {
      return true;
    }
  }

  // Technician can access patients of their gestionnaire
  if (currentUser.role === "technician" && currentUser.gestionnaireId) {
    const targetUser = await ctx.db.get(targetUserId);
    if (targetUser?.gestionnaireId === currentUser.gestionnaireId) {
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

  // Gestionnaire can access their own machines
  if (currentUser.role === "gestionnaire") {
    return machine.gestionnaireId === currentUser._id;
  }

  // Technician can access machines of their gestionnaire
  if (currentUser.role === "technician" && currentUser.gestionnaireId) {
    return machine.gestionnaireId === currentUser.gestionnaireId;
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

  // Only gestionnaire who owns the machine can manage it
  if (currentUser.role === "gestionnaire") {
    return machine.gestionnaireId === currentUser._id;
  }

  // Technicians cannot manage machines
  return false;
}
```

---

## Testing Steps

1. Create the `convex/lib/` directory if it doesn't exist
2. Create `convex/lib/auth.ts` with the implementation
3. Run `npx convex dev` to verify compilation
4. Create a test query to verify each function:

```typescript
// convex/test/authTest.ts (temporary test file)
import { query } from "../_generated/server";
import { v } from "convex/values";
import { requireAuth, getCurrentUserOrThrow, requireRole } from "../lib/auth";

export const testRequireAuth = query({
  args: {},
  returns: v.object({ email: v.optional(v.string()) }),
  handler: async (ctx) => {
    const identity = await requireAuth(ctx);
    return { email: identity.email };
  },
});

export const testRequireRole = query({
  args: { roles: v.array(v.string()) },
  returns: v.object({ role: v.string() }),
  handler: async (ctx, args) => {
    const user = await requireRole(ctx, args.roles as any);
    return { role: user.role };
  },
});
```

5. Test without authentication - verify "Not authenticated" error
6. Test with authenticated user not in database - verify "User not found" error
7. Test with proper user - verify functions return correctly
8. Delete test file after verification

---

## Notes

- These helpers use Convex's built-in `ctx.auth.getUserIdentity()` which integrates with Clerk
- The `subject` field from Clerk identity maps to our `clerkId` field
- All errors are thrown as standard Error objects for consistent handling
- Technicians are linked to a gestionnaire via `gestionnaireId` field
