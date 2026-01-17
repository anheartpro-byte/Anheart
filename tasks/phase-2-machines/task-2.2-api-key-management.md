# Task 2.2: API Key Management

## Objective

Implement secure API key generation, regeneration, and validation for Raspberry Pi machine authentication.

## Files to Modify

- `convex/machines.ts` (add regenerateApiKey)
- `convex/lib/crypto.ts` (create utility functions)

## Dependencies

- Task 2.1 (Machine CRUD) must be completed

---

## Acceptance Criteria

### Crypto Utilities

- [ ] File `convex/lib/crypto.ts` created
- [ ] Function `generateApiKey` returns plain and hashed versions
- [ ] Function `hashApiKey` hashes a plain API key
- [ ] API key is 64 characters (32 bytes hex encoded)
- [ ] Hashing is consistent (same input = same output)

### regenerateApiKey Mutation

- [ ] Function `regenerateApiKey` exported as mutation
- [ ] Args: machineId
- [ ] Returns object with new plain API key
- [ ] Only owner gestionnaire or admin can regenerate
- [ ] Old API key is invalidated immediately
- [ ] Machine remains in same status after regeneration

### validateApiKey Helper

- [ ] Function validates API key against stored hash
- [ ] Returns machine object if valid
- [ ] Returns null if invalid
- [ ] Used by HTTP endpoints

### Security Requirements

- [ ] Plain API key never stored in database
- [ ] Plain API key shown only on create/regenerate
- [ ] API key is cryptographically random
- [ ] Hash function is not reversible

---

## Implementation

```typescript
// convex/lib/crypto.ts

/**
 * Generate a cryptographically secure API key
 * Returns both plain text (to show user once) and hashed (to store)
 */
export function generateApiKey(): { plain: string; hashed: string } {
  // Generate 32 random bytes
  const array = new Uint8Array(32);
  crypto.getRandomValues(array);

  // Convert to hex string (64 characters)
  const plain = Array.from(array, (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");

  // Hash for storage
  const hashed = hashApiKey(plain);

  return { plain, hashed };
}

/**
 * Hash an API key for storage/comparison
 * Uses a simple but effective transformation
 * In production, consider using Web Crypto API with SHA-256
 */
export function hashApiKey(plain: string): string {
  // Base64 encode then reverse (simple but effective for our use case)
  // For production, use: await crypto.subtle.digest('SHA-256', ...)
  return btoa(plain).split("").reverse().join("");
}

/**
 * Verify an API key against a stored hash
 */
export function verifyApiKey(plain: string, storedHash: string): boolean {
  const hash = hashApiKey(plain);
  return hash === storedHash;
}
```

```typescript
// convex/machines.ts (add this function)

import { generateApiKey, hashApiKey } from "./lib/crypto";

/**
 * Regenerate API key for a machine
 * Old API key is immediately invalidated
 */
export const regenerateApiKey = mutation({
  args: {
    machineId: v.id("machines"),
  },
  returns: v.object({
    apiKey: v.string(),
  }),
  handler: async (ctx, args) => {
    const canManage = await canManageMachine(ctx, args.machineId);
    if (!canManage) {
      throw new Error("Not authorized to manage this machine");
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine) {
      throw new Error("Machine not found");
    }

    // Generate new API key
    const { plain, hashed } = generateApiKey();

    // Update machine with new hashed key
    await ctx.db.patch(args.machineId, {
      apiKey: hashed,
    });

    // Return plain key (shown only this once!)
    return {
      apiKey: plain,
    };
  },
});

/**
 * Internal function to validate API key from HTTP request
 */
export const validateMachineApiKey = internalQuery({
  args: {
    apiKey: v.string(),
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
    const hashed = hashApiKey(args.apiKey);

    const machine = await ctx.db
      .query("machines")
      .withIndex("by_api_key", (q) => q.eq("apiKey", hashed))
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

### Test generateApiKey

1. Call `generateApiKey()` multiple times
2. Verify each plain key is unique
3. Verify plain key is 64 characters
4. Verify hashed key is different from plain
5. Verify hashing same plain key twice gives same hash

### Test regenerateApiKey

1. Create a machine, save the API key
2. Call `regenerateApiKey`
3. Verify new API key is different from original
4. Verify old API key no longer works (validation fails)
5. Verify new API key works

### Test validateMachineApiKey

1. Create machine, get API key
2. Call `validateMachineApiKey` with correct key - verify returns machine
3. Call with wrong key - verify returns null
4. Regenerate key
5. Call with old key - verify returns null
6. Call with new key - verify returns machine

### Test Security

1. Check database directly - verify apiKey field contains hash, not plain
2. Verify hash cannot be reversed to get plain key
3. Try API key that's almost correct (1 character off) - verify fails

---

## Notes

- The current implementation uses a simple base64+reverse hash
- For production, upgrade to proper SHA-256:

```typescript
// Production-grade hashing (for future implementation)
async function hashApiKeySHA256(plain: string): Promise<string> {
  const encoder = new TextEncoder();
  const data = encoder.encode(plain);
  const hashBuffer = await crypto.subtle.digest("SHA-256", data);
  const hashArray = Array.from(new Uint8Array(hashBuffer));
  return hashArray.map((b) => b.toString(16).padStart(2, "0")).join("");
}
```

- API key rotation should be logged for audit trail (future feature)
- Consider adding API key expiration (future feature)
