# Task 3.2: ECG Data Streaming

## Objective

Implement HTTP endpoint for receiving ECG data batches from Raspberry Pi and storing them in the database.

## Files to Modify

- `convex/http.ts` - Add data streaming endpoint
- `convex/ecgData.ts` - Create data storage functions

## Dependencies

- Task 3.1 (Session CRUD) must be completed
- Phase 2 (Machines) must be completed

---

## Acceptance Criteria

### HTTP Data Endpoint

- [ ] Route `POST /api/machine/data` created
- [ ] Validates Authorization header (Bearer token)
- [ ] Returns 401 if API key missing or invalid
- [ ] Validates required fields: sessionId, timestamp, samples
- [ ] Validates timestamp is not in future (with 60s tolerance)
- [ ] Validates timestamp is not too old (5 min tolerance)
- [ ] Returns 200 with acknowledgment on success
- [ ] Returns 400 with error message on validation failure

### storeEcgBatch Internal Mutation

- [ ] Function `storeEcgBatch` exported as internalMutation
- [ ] Args: machineId, sessionId, timestamp, samples
- [ ] Validates session exists and is "active"
- [ ] Validates session belongs to the machine
- [ ] Stores batch in ecg_data table
- [ ] Returns null on success

### HTTP Polling Endpoint

- [ ] Route `GET /api/machine/session/poll` created
- [ ] Returns pending session info if available
- [ ] Returns `{ session: null }` if no pending session
- [ ] Includes session channels and config

### HTTP Session Start Endpoint

- [ ] Route `POST /api/machine/session/start` created
- [ ] Args: sessionId
- [ ] Calls internal startSession mutation
- [ ] Returns success response

### HTTP Session End Endpoint

- [ ] Route `POST /api/machine/session/end` created
- [ ] Args: sessionId, reason (optional)
- [ ] Ends or fails the session
- [ ] Returns success response

### Data Format

- [ ] Samples array: `[{ channel: "ECG", values: [1000 numbers] }]`
- [ ] Timestamp in milliseconds (Unix epoch)
- [ ] Each batch represents 1 second of data

---

## Implementation

```typescript
// convex/ecgData.ts
import { internalMutation, internalQuery } from "./_generated/server";
import { v } from "convex/values";

/**
 * Store a batch of ECG data from RPi
 */
export const storeEcgBatch = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    timestamp: v.number(),
    samples: v.array(
      v.object({
        channel: v.string(),
        values: v.array(v.number()),
      }),
    ),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    // Validate session exists
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    // Validate session is active
    if (session.status !== "active") {
      throw new Error(`Session is not active (status: ${session.status})`);
    }

    // Validate session belongs to this machine
    if (session.machineId !== args.machineId) {
      throw new Error("Session does not belong to this machine");
    }

    // Store the data batch
    await ctx.db.insert("ecg_data", {
      sessionId: args.sessionId,
      timestamp: args.timestamp,
      samples: args.samples,
    });

    return null;
  },
});

/**
 * Get data count for a session (for debugging)
 */
export const getSessionDataCount = internalQuery({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.number(),
  handler: async (ctx, args) => {
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();
    return data.length;
  },
});

/**
 * Get all ECG data for a session (for summary generation)
 */
export const getAllSessionData = internalQuery({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.array(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
  ),
  handler: async (ctx, args) => {
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();

    return data.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});
```

```typescript
// convex/http.ts (add these routes to existing file)

/**
 * POST /api/machine/data
 * Receive ECG data batch from Raspberry Pi
 */
http.route({
  path: "/api/machine/data",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;

    // Parse body
    let body: {
      sessionId?: string;
      timestamp?: number;
      samples?: Array<{ channel: string; values: number[] }>;
      batchId?: string;
    };

    try {
      body = await req.json();
    } catch (e) {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    // Validate required fields
    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.timestamp) {
      return new Response(JSON.stringify({ error: "Missing timestamp" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.samples || !Array.isArray(body.samples)) {
      return new Response(
        JSON.stringify({ error: "Missing or invalid samples array" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Validate timestamp
    const now = Date.now();
    const maxFuture = now + 60000; // 1 minute in future
    const maxPast = now - 300000; // 5 minutes in past

    if (body.timestamp > maxFuture) {
      return new Response(
        JSON.stringify({ error: "Timestamp too far in future" }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    if (body.timestamp < maxPast) {
      return new Response(
        JSON.stringify({
          error: "Timestamp too old. Data must be less than 5 minutes old.",
        }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    // Store the data
    try {
      await ctx.runMutation(internal.ecgData.storeEcgBatch, {
        machineId: machine._id,
        sessionId: body.sessionId as any,
        timestamp: body.timestamp,
        samples: body.samples,
      });

      return new Response(
        JSON.stringify({
          success: true,
          batchId: body.batchId,
          serverTime: now,
        }),
        {
          status: 200,
          headers: { "Content-Type": "application/json" },
        },
      );
    } catch (error: any) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
  }),
});

/**
 * GET /api/machine/session/poll
 * RPi polls for pending sessions
 */
http.route({
  path: "/api/machine/session/poll",
  method: "GET",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }
    const { machine } = authResult;

    // Check for pending session
    const pendingSession = await ctx.runQuery(
      internal.sessions.getPendingSessionForMachine,
      { machineId: machine._id },
    );

    if (!pendingSession) {
      return new Response(JSON.stringify({ session: null }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response(
      JSON.stringify({
        session: {
          id: pendingSession._id,
          channels: pendingSession.channels,
          config: pendingSession.config,
        },
      }),
      {
        status: 200,
        headers: { "Content-Type": "application/json" },
      },
    );
  }),
});

/**
 * POST /api/machine/session/start
 * RPi notifies it has started a session
 */
http.route({
  path: "/api/machine/session/start",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }

    // Parse body
    let body: { sessionId?: string };
    try {
      body = await req.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      await ctx.runMutation(internal.sessions.startSession, {
        sessionId: body.sessionId as any,
      });

      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } catch (error: any) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
  }),
});

/**
 * POST /api/machine/session/end
 * RPi notifies session has ended (or failed)
 */
http.route({
  path: "/api/machine/session/end",
  method: "POST",
  handler: httpAction(async (ctx, req) => {
    // Validate API key
    const authResult = await validateMachineAuth(ctx, req);
    if ("error" in authResult) {
      return authResult.error;
    }

    // Parse body
    let body: { sessionId?: string; reason?: string; failed?: boolean };
    try {
      body = await req.json();
    } catch {
      return new Response(JSON.stringify({ error: "Invalid JSON body" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    if (!body.sessionId) {
      return new Response(JSON.stringify({ error: "Missing sessionId" }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }

    try {
      if (body.failed) {
        // Session failed
        await ctx.runMutation(internal.sessions.failSession, {
          sessionId: body.sessionId as any,
          reason: body.reason ?? "Unknown error",
        });
      } else {
        // Session completed normally - trigger via internal since we're from HTTP
        // Note: This requires adding an internal endSession or using scheduler
        await ctx.runMutation(internal.sessions.endSessionInternal, {
          sessionId: body.sessionId as any,
        });
      }

      return new Response(JSON.stringify({ success: true }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    } catch (error: any) {
      return new Response(JSON.stringify({ error: error.message }), {
        status: 400,
        headers: { "Content-Type": "application/json" },
      });
    }
  }),
});
```

```typescript
// convex/sessions.ts (add this internal mutation)

/**
 * End session internally (from HTTP endpoint)
 */
export const endSessionInternal = internalMutation({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      throw new Error("Session not found");
    }

    if (session.status !== "active") {
      throw new Error(`Session is not active (status: ${session.status})`);
    }

    const now = Date.now();

    // Update session
    await ctx.db.patch(args.sessionId, {
      status: "completed",
      endedAt: now,
    });

    // Update machine status
    await ctx.db.patch(session.machineId, {
      status: "online",
    });

    // Schedule summary generation
    await ctx.scheduler.runAfter(0, internal.sessionSummaries.generateSummary, {
      sessionId: args.sessionId,
    });

    return null;
  },
});
```

---

## Testing Steps

### Test Data Endpoint

Using curl:

```bash
API_KEY="your-api-key"
CONVEX_URL="https://your-project.convex.cloud"
SESSION_ID="your-session-id"

# Test successful data upload
curl -X POST "${CONVEX_URL}/api/machine/data" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "sessionId": "'${SESSION_ID}'",
    "timestamp": '$(date +%s000)',
    "samples": [{"channel": "ECG", "values": [100, 102, 105, 103, 101]}],
    "batchId": "batch-001"
  }'

# Expected: {"success":true,"batchId":"batch-001","serverTime":...}

# Test with invalid session
curl -X POST "${CONVEX_URL}/api/machine/data" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "sessionId": "invalid-id",
    "timestamp": '$(date +%s000)',
    "samples": [{"channel": "ECG", "values": [100]}]
  }'

# Expected: 400 error

# Test with old timestamp
curl -X POST "${CONVEX_URL}/api/machine/data" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{
    "sessionId": "'${SESSION_ID}'",
    "timestamp": '$(($(date +%s000) - 600000))',
    "samples": [{"channel": "ECG", "values": [100]}]
  }'

# Expected: {"error":"Timestamp too old..."}
```

### Test Polling Endpoint

```bash
# Poll for session
curl -X GET "${CONVEX_URL}/api/machine/session/poll" \
  -H "Authorization: Bearer ${API_KEY}"

# If pending: {"session":{"id":"...","channels":["ECG"],"config":{...}}}
# If none: {"session":null}
```

### Test Session Lifecycle

```bash
# 1. Create session via web UI (status: pending)
# 2. Poll for session
curl -X GET "${CONVEX_URL}/api/machine/session/poll" \
  -H "Authorization: Bearer ${API_KEY}"

# 3. Start session
curl -X POST "${CONVEX_URL}/api/machine/session/start" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "SESSION_ID"}'

# 4. Send data batches (repeat)
# 5. End session
curl -X POST "${CONVEX_URL}/api/machine/session/end" \
  -H "Authorization: Bearer ${API_KEY}" \
  -H "Content-Type: application/json" \
  -d '{"sessionId": "SESSION_ID"}'
```

---

## Notes

- Each batch should contain ~1 second of data (1000 samples at 1000 Hz)
- RPi should send heartbeats alongside data uploads
- Timestamp validation allows for network delays
- batchId is optional but helps with debugging/deduplication
- Multi-channel data supported (ECG, EMG, etc.)
