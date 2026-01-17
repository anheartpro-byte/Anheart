# Task 2.3: Heartbeat System

## Objective

Implement HTTP endpoint for machine heartbeats and automatic offline detection via cron job.

## Files to Create/Modify

- `convex/http.ts` - HTTP endpoint for heartbeat
- `convex/machines.ts` - Add heartbeat recording function
- `convex/crons.ts` - Scheduled offline detection

## Dependencies

- Task 2.1 (Machine CRUD) must be completed
- Task 2.2 (API Key Management) must be completed

---

## Acceptance Criteria

### HTTP Heartbeat Endpoint

- [ ] Route `POST /api/machine/heartbeat` created
- [ ] Validates Authorization header (Bearer token)
- [ ] Returns 401 if API key missing or invalid
- [ ] Returns 200 with success response if valid
- [ ] Accepts optional body: batteryLevel, wifiStrength, activeSessionId

### recordHeartbeat Internal Mutation

- [ ] Function `recordHeartbeat` exported as internalMutation
- [ ] Updates machine.lastHeartbeat to current timestamp
- [ ] Updates machine.status to "online" (or "in_session" if active session)
- [ ] Stores heartbeat in machine_heartbeats table
- [ ] Stores optional battery and wifi info

### checkOfflineMachines Internal Mutation

- [ ] Function `checkOfflineMachines` exported as internalMutation
- [ ] Finds machines with status "online" or "in_session"
- [ ] Checks if lastHeartbeat is older than 90 seconds
- [ ] Updates stale machines to status "offline"
- [ ] Does NOT mark sessions as failed (handled separately)

### Cron Job

- [ ] Cron job runs every 1 minute
- [ ] Calls `checkOfflineMachines`
- [ ] Registered in `convex/crons.ts`

### HTTP Response Format

- [ ] Success: `{ "success": true, "serverTime": <timestamp> }`
- [ ] Auth error: `{ "error": "Missing Authorization header" }` or `{ "error": "Invalid API key" }`
- [ ] Status codes: 200 (success), 401 (auth error)

---

## Implementation

```typescript
// convex/http.ts
import { httpRouter } from "convex/server";
import { httpAction } from "./_generated/server";
import { internal } from "./_generated/api";
import { hashApiKey } from "./lib/crypto";

const http = httpRouter();

/**
 * Helper to validate machine API key from request
 */
async function validateMachineAuth(
  ctx: any,
  req: Request,
): Promise<{ machine: any } | { error: Response }> {
  const authHeader = req.headers.get("Authorization");

  if (!authHeader || !authHeader.startsWith("Bearer ")) {
    return {
      error: new Response(
        JSON.stringify({ error: "Missing Authorization header" }),
        {
          status: 401,
          headers: { "Content-Type": "application/json" },
        },
      ),
    };
  }

  const apiKey = authHeader.substring(7); // Remove "Bearer "
  const apiKeyHash = hashApiKey(apiKey);

  const machine = await ctx.runQuery(internal.machines.getMachineByApiKey, {
    apiKeyHash,
  });

  if (!machine) {
    return {
      error: new Response(JSON.stringify({ error: "Invalid API key" }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    };
  }

  return { machine };
}

/**
 * POST /api/machine/heartbeat
 * Raspberry Pi sends this every 30 seconds
 */
http.route({
  path: "/api/machine/heartbeat",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;

    // Parse optional body
    let body: {
      batteryLevel?: number;
      wifiStrength?: number;
      activeSessionId?: string;
    } = {};

    try {
      const text = await req.text();
      if (text) {
        body = JSON.parse(text);
      }
    } catch {
      // Empty or invalid body is OK - use defaults
    }

    // Record the heartbeat
    await ctx.runMutation(internal.machines.recordHeartbeat, {
      machineId: machine._id,
      batteryLevel: body.batteryLevel,
      wifiStrength: body.wifiStrength,
      activeSessionId: body.activeSessionId,
    });

    return new Response(
      JSON.stringify({
        success: true,
        serverTime: Date.now(),
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

export default http;
```

```typescript
// convex/machines.ts (add these functions)

import { internalMutation } from "./_generated/server";

/**
 * Record a heartbeat from a machine
 */
export const recordHeartbeat = internalMutation({
  args: {
    machineId: v.id("machines"),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.string()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const now = Date.now();
    const machine = await ctx.db.get(args.machineId);

    if (!machine) {
      throw new Error("Machine not found");
    }

    // Determine new status
    let newStatus: "online" | "in_session" = "online";
    if (args.activeSessionId) {
      newStatus = "in_session";
    }

    // Update machine
    await ctx.db.patch(args.machineId, {
      lastHeartbeat: now,
      status: newStatus,
    });

    // Record heartbeat history
    await ctx.db.insert("machine_heartbeats", {
      machineId: args.machineId,
      timestamp: now,
      batteryLevel: args.batteryLevel,
      wifiStrength: args.wifiStrength,
      activeSessionId: args.activeSessionId
        ? (args.activeSessionId as any)
        : undefined,
    });

    return null;
  },
});

/**
 * Check for machines that have gone offline
 * Called by cron job every minute
 */
export const checkOfflineMachines = internalMutation({
  args: {},
  returns: v.object({
    checkedCount: v.number(),
    markedOfflineCount: v.number(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const cutoff = now - 90000; // 90 seconds ago

    // Get online machines
    const onlineMachines = await ctx.db
      .query("machines")
      .withIndex("by_status", (q) => q.eq("status", "online"))
      .collect();

    // Get in_session machines
    const inSessionMachines = await ctx.db
      .query("machines")
      .withIndex("by_status", (q) => q.eq("status", "in_session"))
      .collect();

    const allActiveMachines = [...onlineMachines, ...inSessionMachines];
    let markedOfflineCount = 0;

    for (const machine of allActiveMachines) {
      if (machine.lastHeartbeat < cutoff) {
        await ctx.db.patch(machine._id, { status: "offline" });
        markedOfflineCount++;

        // If machine was in session, we should handle that
        // This will be expanded in Phase 3 to mark sessions as failed
        console.log(
          `Machine ${machine.name} marked offline (last heartbeat: ${new Date(machine.lastHeartbeat).toISOString()})`,
        );
      }
    }

    return {
      checkedCount: allActiveMachines.length,
      markedOfflineCount,
    };
  },
});

/**
 * Get recent heartbeats for a machine (for debugging/monitoring)
 */
export const getRecentHeartbeats = query({
  args: {
    machineId: v.id("machines"),
    limit: v.optional(v.number()),
  },
  returns: v.array(
    v.object({
      _id: v.id("machine_heartbeats"),
      timestamp: v.number(),
      batteryLevel: v.optional(v.number()),
      wifiStrength: v.optional(v.number()),
      activeSessionId: v.optional(v.id("sessions")),
    }),
  ),
  handler: async (ctx, args) => {
    const hasAccess = await canAccessMachine(ctx, args.machineId);
    if (!hasAccess) {
      return [];
    }

    const limit = args.limit ?? 10;

    const heartbeats = await ctx.db
      .query("machine_heartbeats")
      .withIndex("by_machine_and_timestamp", (q) =>
        q.eq("machineId", args.machineId),
      )
      .order("desc")
      .take(limit);

    return heartbeats.map((h) => ({
      _id: h._id,
      timestamp: h.timestamp,
      batteryLevel: h.batteryLevel,
      wifiStrength: h.wifiStrength,
      activeSessionId: h.activeSessionId,
    }));
  },
});
```

```typescript
// convex/crons.ts
import { cronJobs } from "convex/server";
import { internal } from "./_generated/api";

const crons = cronJobs();

/**
 * Check for offline machines every minute
 */
crons.interval(
  "check-offline-machines",
  { minutes: 1 },
  internal.machines.checkOfflineMachines,
  {},
);

export default crons;
```

---

## Testing Steps

### Test Heartbeat Endpoint

Using curl:

```bash
# Get your API key from machine creation
API_KEY="your-64-char-api-key"
CONVEX_URL="https://your-project.convex.cloud"

# Test successful heartbeat
curl -X POST "${CONVEX_URL}/api/machine/heartbeat" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"batteryLevel": 85, "wifiStrength": -45}'

# Expected response: {"success":true,"serverTime":1234567890123}

# Test without auth header
curl -X POST "${CONVEX_URL}/api/machine/heartbeat"
# Expected response: {"error":"Missing Authorization header"} (401)

# Test with invalid API key
curl -X POST "${CONVEX_URL}/api/machine/heartbeat" \
  -H "Authorization: Bearer invalid-key"
# Expected response: {"error":"Invalid API key"} (401)
```

### Test Machine Status Updates

1. Create machine - verify status is "offline"
2. Send heartbeat - verify status changes to "online"
3. Check lastHeartbeat timestamp is updated
4. Send heartbeat with activeSessionId - verify status is "in_session"

### Test Offline Detection

1. Create machine
2. Send heartbeat - status becomes "online"
3. Wait 2 minutes (or manually trigger cron)
4. Check machine status - should be "offline"

### Test Heartbeat History

1. Send 5 heartbeats with different battery levels
2. Query `getRecentHeartbeats` - verify all 5 appear
3. Verify they're in descending order (newest first)

---

## Notes

- Heartbeat interval from RPi should be 30 seconds
- Offline detection threshold is 90 seconds (3 missed heartbeats)
- Cron runs every 60 seconds for balance between accuracy and cost
- Battery level is 0-100, wifi strength is in dBm (negative)
- HTTP endpoint is publicly accessible but requires valid API key
- Consider adding rate limiting in production
