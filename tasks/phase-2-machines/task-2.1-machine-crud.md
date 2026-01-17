# Task 2.1: Machine CRUD Operations

## Objective

Implement all machine management functions including create, read, update, delete with proper authorization.

## File to Create

`convex/machines.ts`

## Dependencies

- Phase 1 completed (schema, auth helpers, user management)

---

## Acceptance Criteria

### createMachine Mutation

- [ ] Function `createMachine` exported as mutation
- [ ] Args: name (string), location (optional string), config (optional object)
- [ ] Returns object with machineId and plain API key
- [ ] Only admin and gestionnaire can create machines
- [ ] Sets gestionnaireId to current user
- [ ] Status defaults to "offline"
- [ ] lastHeartbeat set to 0
- [ ] Default config: sampleRate=1000, channels=["ECG"], batchInterval=1000
- [ ] Generates and stores hashed API key

### getMachine Query

- [ ] Function `getMachine` exported as query
- [ ] Args: machineId
- [ ] Returns machine object without API key, or null
- [ ] Respects access control (canAccessMachine)
- [ ] Includes all fields except apiKey

### listMachines Query

- [ ] Function `listMachines` exported as query
- [ ] Args: status (optional filter)
- [ ] Returns array of machine summaries
- [ ] Admin sees all machines
- [ ] Gestionnaire sees only their machines
- [ ] Technician sees their gestionnaire's machines
- [ ] User sees nothing (empty array)

### updateMachine Mutation

- [ ] Function `updateMachine` exported as mutation
- [ ] Args: machineId, name (optional), location (optional), config (optional)
- [ ] Returns null
- [ ] Only owner gestionnaire or admin can update
- [ ] Only updates provided fields

### deleteMachine Mutation

- [ ] Function `deleteMachine` exported as mutation
- [ ] Args: machineId
- [ ] Returns null
- [ ] Only owner gestionnaire or admin can delete
- [ ] Cannot delete machine with active session
- [ ] Deletes machine from database

### getMachineByApiKey Internal Query

- [ ] Function `getMachineByApiKey` exported as internalQuery
- [ ] Args: apiKeyHash (string)
- [ ] Returns machine object or null
- [ ] Used by HTTP endpoints for authentication

---

## Implementation

```typescript
// convex/machines.ts
import { query, mutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";
import {
  requireRole,
  getCurrentUserOrThrow,
  canAccessMachine,
  canManageMachine,
} from "./lib/auth";

/**
 * Generate a random API key
 * Returns both plain text (to show user) and hashed (to store)
 */
function generateApiKey(): { plain: string; hashed: string } {
  // Generate 32 random bytes as hex (64 characters)
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);
  const plain = Array.from(array, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");

  // Simple hash for storage (in production, use proper crypto)
  const hashed = btoa(plain).split("").reverse().join("");

  return { plain, hashed };
}

/**
 * Hash an API key for comparison
 */
function hashApiKey(plain: string): string {
  return btoa(plain).split("").reverse().join("");
}

/**
 * Create a new machine (Raspberry Pi)
 */
export const createMachine = mutation({
  args: {
    name: v.string(),
    location: v.optional(v.string()),
    config: v.optional(
      v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    ),
  },
  returns: v.object({
    machineId: v.id("machines"),
    apiKey: v.string(),
  }),
  handler: async (ctx, args) => {
    const currentUser = await requireRole(ctx, ["admin", "gestionnaire"]);

    const { plain, hashed } = generateApiKey();

    const machineId = await ctx.db.insert("machines", {
      name: args.name,
      apiKey: hashed,
      gestionnaireId: currentUser._id,
      status: "offline",
      lastHeartbeat: 0,
      location: args.location,
      config: args.config ?? {
        sampleRate: 1000,
        channels: ["ECG"],
        batchInterval: 1000,
      },
      createdAt: Date.now(),
    });

    // Return plain API key - this is the only time it's visible!
    return {
      machineId,
      apiKey: plain,
    };
  },
});

/**
 * Get machine details (without API key)
 */
export const getMachine = query({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.union(
    v.object({
      _id: v.id("machines"),
      _creationTime: v.number(),
      name: v.string(),
      gestionnaireId: v.id("users"),
      status: v.union(
        v.literal("online"),
        v.literal("offline"),
        v.literal("in_session"),
      ),
      lastHeartbeat: v.number(),
      location: v.optional(v.string()),
      config: v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const hasAccess = await canAccessMachine(ctx, args.machineId);
    if (!hasAccess) {
      return null;
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;

    // Return without API key
    return {
      _id: machine._id,
      _creationTime: machine._creationTime,
      name: machine.name,
      gestionnaireId: machine.gestionnaireId,
      status: machine.status,
      lastHeartbeat: machine.lastHeartbeat,
      location: machine.location,
      config: machine.config,
      createdAt: machine.createdAt,
    };
  },
});

/**
 * List machines with role-based filtering
 */
export const listMachines = query({
  args: {
    status: v.optional(
      v.union(
        v.literal("online"),
        v.literal("offline"),
        v.literal("in_session"),
      ),
    ),
  },
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      status: v.string(),
      lastHeartbeat: v.number(),
      location: v.optional(v.string()),
      gestionnaireId: v.id("users"),
    }),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    let machines;

    if (currentUser.role === "admin") {
      // Admin sees all machines
      if (args.status) {
        machines = await ctx.db
          .query("machines")
          .withIndex("by_status", (q) => q.eq("status", args.status!))
          .collect();
      } else {
        machines = await ctx.db.query("machines").collect();
      }
    } else if (currentUser.role === "gestionnaire") {
      // Gestionnaire sees their own machines
      machines = await ctx.db
        .query("machines")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser._id),
        )
        .collect();

      if (args.status) {
        machines = machines.filter((m) => m.status === args.status);
      }
    } else if (
      currentUser.role === "technician" &&
      currentUser.gestionnaireId
    ) {
      // Technician sees their gestionnaire's machines
      machines = await ctx.db
        .query("machines")
        .withIndex("by_gestionnaire", (q) =>
          q.eq("gestionnaireId", currentUser.gestionnaireId!),
        )
        .collect();

      if (args.status) {
        machines = machines.filter((m) => m.status === args.status);
      }
    } else {
      // Users don't see machines
      return [];
    }

    return machines.map((m) => ({
      _id: m._id,
      name: m.name,
      status: m.status,
      lastHeartbeat: m.lastHeartbeat,
      location: m.location,
      gestionnaireId: m.gestionnaireId,
    }));
  },
});

/**
 * Update machine configuration
 */
export const updateMachine = mutation({
  args: {
    machineId: v.id("machines"),
    name: v.optional(v.string()),
    location: v.optional(v.string()),
    config: v.optional(
      v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    ),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const canManage = await canManageMachine(ctx, args.machineId);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    const updates: Record<string, any> = {};
    if (args.name !== undefined) updates.name = args.name;
    if (args.location !== undefined) updates.location = args.location;
    if (args.config !== undefined) updates.config = args.config;

    if (Object.keys(updates).length > 0) {
      await ctx.db.patch(args.machineId, updates);
    }

    return null;
  },
});

/**
 * Delete a machine
 */
export const deleteMachine = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const canManage = await canManageMachine(ctx, args.machineId);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Check for active sessions
    if (machine.status === "in_session") {
      throw new Error("Cannot delete machine with active session");
    }

    // Check for any pending sessions
    const pendingSessions = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .first();

    if (pendingSessions) {
      throw new Error("Cannot delete machine with pending sessions");
    }

    await ctx.db.delete(args.machineId);
    return null;
  },
});

/**
 * Get machine by API key hash (for HTTP endpoint auth)
 */
export const getMachineByApiKey = internalQuery({
  args: {
    apiKeyHash: v.string(),
  },
  returns: v.union(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      gestionnaireId: v.id("users"),
      status: v.string(),
      config: v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const machine = await ctx.db
      .query("machines")
      .withIndex("by_api_key", (q) => q.eq("apiKey", args.apiKeyHash))
      .unique();

    if (!machine) return null;

    return {
      _id: machine._id,
      name: machine.name,
      gestionnaireId: machine.gestionnaireId,
      status: machine.status,
      config: machine.config,
    };
  },
});
```

---

## Testing Steps

### Test createMachine

1. Login as gestionnaire
2. Call `createMachine` with name "Test RPi"
3. Verify machine created in database
4. Verify API key is returned (64 character hex string)
5. Verify API key is NOT stored in plain text (check database)

### Test getMachine

1. Get machine as owner - verify returns full details
2. Get machine as non-owner - verify returns null
3. Get machine as admin - verify returns details

### Test listMachines

1. Create 3 machines as gestionnaire
2. List as same gestionnaire - verify sees all 3
3. List as different gestionnaire - verify sees 0
4. List as admin - verify sees all
5. List with status filter - verify filtering works

### Test updateMachine

1. Update machine name as owner - verify success
2. Update config - verify new config saved
3. Try update as non-owner - verify error

### Test deleteMachine

1. Delete machine with no sessions - verify success
2. Try delete machine in session - verify error
3. Try delete as non-owner - verify error

---

## Notes

- API key is shown only once during creation - user must save it
- API key is stored as a hash, not plain text
- In production, consider using proper bcrypt or argon2 for hashing
- Machine status is managed by heartbeat system (Task 2.3)
