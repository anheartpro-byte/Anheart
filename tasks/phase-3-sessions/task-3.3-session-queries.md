# Task 3.3: Session Data Queries

## Objective

Implement queries for retrieving ECG data with role-based access control and delayed view for gestionnaires.

## File to Modify

`convex/ecgData.ts`

## Dependencies

- Task 3.1 (Session CRUD) completed
- Task 3.2 (ECG Data Streaming) completed

---

## Acceptance Criteria

### getRecentEcgData Query

- [ ] Function `getRecentEcgData` exported as query
- [ ] Args: sessionId, seconds (optional, default 10)
- [ ] Returns array of timestamped data batches
- [ ] Patient sees real-time data (no delay)
- [ ] Admin sees real-time data (no delay)
- [ ] Gestionnaire sees 5-second delayed data
- [ ] Technician sees 5-second delayed data
- [ ] Respects session access control

### getSessionEcgRange Query

- [ ] Function `getSessionEcgRange` exported as query
- [ ] Args: sessionId, startTime, endTime
- [ ] Returns data within time range
- [ ] For completed sessions (historical view)
- [ ] No delay for historical data
- [ ] Respects access control

### Access Control

- [ ] Unauthorized users get empty array
- [ ] Patient can only view own sessions
- [ ] Gestionnaire can view their machine's sessions
- [ ] Admin can view all sessions

### Delay Logic

- [ ] 5-second delay applied to gestionnaire/technician live view
- [ ] Delay filters out data with timestamp > (now - 5000)
- [ ] No delay for patient viewing own session
- [ ] No delay for completed sessions

---

## Implementation

```typescript
// convex/ecgData.ts (add these queries)

import { query } from "./_generated/server";
import { v } from "convex/values";
import { getCurrentUserOrThrow, canAccessMachine } from "./lib/auth";

/**
 * Get recent ECG data for live viewing
 * Includes 5-second delay for gestionnaire/technician
 */
export const getRecentEcgData = query({
  args: {
    sessionId: v.id("sessions"),
    seconds: v.optional(v.number()),
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
    const currentUser = await getCurrentUserOrThrow(ctx);
    const seconds = args.seconds ?? 10;
    const now = Date.now();

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return [];
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return [];
    }

    // Determine delay
    // No delay for: patient, admin, or completed sessions
    // 5s delay for: gestionnaire, technician viewing active sessions
    let delay = 0;
    if (!isPatient && !isAdmin && session.status === "active") {
      delay = 5000; // 5 seconds
    }

    // Calculate time window
    const maxTime = now - delay;
    const minTime = maxTime - seconds * 1000;

    // Query data
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId).gt("timestamp", minTime),
      )
      .collect();

    // Filter by max time (for delay)
    const filteredData = data.filter((d) => d.timestamp <= maxTime);

    // Sort by timestamp ascending
    filteredData.sort((a, b) => a.timestamp - b.timestamp);

    return filteredData.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});

/**
 * Get ECG data for a time range (historical view)
 */
export const getSessionEcgRange = query({
  args: {
    sessionId: v.id("sessions"),
    startTime: v.number(),
    endTime: v.number(),
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
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return [];
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return [];
    }

    // Query data in range
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q
          .eq("sessionId", args.sessionId)
          .gt("timestamp", args.startTime)
          .lt("timestamp", args.endTime),
      )
      .collect();

    // Sort by timestamp ascending
    data.sort((a, b) => a.timestamp - b.timestamp);

    return data.map((d) => ({
      timestamp: d.timestamp,
      samples: d.samples,
    }));
  },
});

/**
 * Get session data stats (for UI display)
 */
export const getSessionDataStats = query({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(
    v.object({
      totalBatches: v.number(),
      firstTimestamp: v.optional(v.number()),
      lastTimestamp: v.optional(v.number()),
      channels: v.array(v.string()),
      durationSeconds: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return null;
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get all data (consider pagination for very long sessions)
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .collect();

    if (data.length === 0) {
      return {
        totalBatches: 0,
        firstTimestamp: undefined,
        lastTimestamp: undefined,
        channels: session.channels,
        durationSeconds: 0,
      };
    }

    // Sort to find first and last
    data.sort((a, b) => a.timestamp - b.timestamp);
    const firstTimestamp = data[0].timestamp;
    const lastTimestamp = data[data.length - 1].timestamp;
    const durationSeconds = Math.round((lastTimestamp - firstTimestamp) / 1000);

    // Get unique channels from actual data
    const channelsSet = new Set<string>();
    for (const batch of data) {
      for (const sample of batch.samples) {
        channelsSet.add(sample.channel);
      }
    }

    return {
      totalBatches: data.length,
      firstTimestamp,
      lastTimestamp,
      channels: Array.from(channelsSet),
      durationSeconds,
    };
  },
});

/**
 * Subscribe to latest ECG data (for real-time updates)
 * Returns the most recent batch only
 */
export const getLatestEcgBatch = query({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.union(
    v.object({
      timestamp: v.number(),
      samples: v.array(
        v.object({
          channel: v.string(),
          values: v.array(v.number()),
        }),
      ),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);
    const now = Date.now();

    // Get session
    const session = await ctx.db.get(args.sessionId);
    if (!session) {
      return null;
    }

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Determine delay
    let delay = 0;
    if (!isPatient && !isAdmin && session.status === "active") {
      delay = 5000;
    }

    const maxTime = now - delay;

    // Get latest batch (within delay window)
    const data = await ctx.db
      .query("ecg_data")
      .withIndex("by_session_and_timestamp", (q) =>
        q.eq("sessionId", args.sessionId),
      )
      .order("desc")
      .first();

    if (!data || data.timestamp > maxTime) {
      return null;
    }

    return {
      timestamp: data.timestamp,
      samples: data.samples,
    };
  },
});
```

---

## Testing Steps

### Test getRecentEcgData

1. **Setup**: Create session with data (at least 30 seconds worth)

2. **Test as patient**:
   - Login as the session's patient
   - Call `getRecentEcgData` - verify returns most recent data
   - Verify no delay (data includes up to current time)

3. **Test as gestionnaire**:
   - Login as machine's gestionnaire
   - Call `getRecentEcgData` - verify returns data
   - Verify 5-second delay (no data from last 5 seconds)

4. **Test as admin**:
   - Login as admin
   - Call `getRecentEcgData` - verify returns all data (no delay)

5. **Test unauthorized**:
   - Login as different gestionnaire
   - Call `getRecentEcgData` - verify returns empty array

### Test Delay Logic

```typescript
// Example test scenario
const now = Date.now();

// Data exists for: now-10s, now-8s, now-6s, now-4s, now-2s, now

// Patient sees: all 6 batches
// Gestionnaire sees: now-10s, now-8s, now-6s (3 batches, 5s delay)
```

### Test Historical View

1. End a session
2. Query `getSessionEcgRange` with startTime/endTime
3. Verify returns correct data range
4. Verify no delay for completed session

---

## Notes

- Live view uses subscription (Convex auto-updates on data change)
- Delay is enforced server-side, cannot be bypassed by client
- For very long sessions, consider pagination
- Channel names should be consistent ("ECG", "EMG", etc.)
- Frontend should handle reconnection gracefully
