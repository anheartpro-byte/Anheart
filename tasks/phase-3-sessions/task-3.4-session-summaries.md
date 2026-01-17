# Task 3.4: Session Summaries

## Objective

Implement post-session analytics including heart rate calculation, HRV analysis, and ECG downsampling for historical view.

## File to Create

`convex/sessionSummaries.ts`

## Dependencies

- Task 3.1 (Session CRUD) completed
- Task 3.2 (ECG Data Streaming) completed

---

## Acceptance Criteria

### generateSummary Internal Action

- [ ] Function `generateSummary` exported as internalAction
- [ ] Args: sessionId
- [ ] Retrieves all ECG data for the session
- [ ] Calculates session duration
- [ ] Calculates average, min, max heart rate
- [ ] Calculates HRV (RMSSD method)
- [ ] Creates downsampled ECG (1 sample per second)
- [ ] Stores summary in session_summaries table
- [ ] Handles empty sessions gracefully (zero values)
- [ ] Completes within 30 seconds for 1-hour session

### Heart Rate Calculation

- [ ] Uses R-R interval detection algorithm
- [ ] Filters out invalid heart rates (< 30 or > 220 BPM)
- [ ] Returns 0 values if insufficient data

### HRV Calculation

- [ ] Uses RMSSD (Root Mean Square of Successive Differences)
- [ ] Requires at least 2 R-R intervals
- [ ] Returns 0 if insufficient data

### Downsampling

- [ ] Reduces to 1 data point per second
- [ ] Takes average of values in each second
- [ ] Preserves timestamp information
- [ ] Limits to 3600 points max (1 hour)

### getSummary Query

- [ ] Function `getSummary` exported as query
- [ ] Args: sessionId
- [ ] Returns summary object or null
- [ ] Respects access control

### getSummaryWithEcg Query

- [ ] Function `getSummaryWithEcg` exported as query
- [ ] Returns summary with downsampled ECG for chart
- [ ] Used for session report page

---

## Implementation

```typescript
// convex/sessionSummaries.ts
import {
  query,
  internalAction,
  internalMutation,
  internalQuery,
} from "./_generated/server";
import { v } from "convex/values";
import { internal } from "./_generated/api";
import { getCurrentUserOrThrow, canAccessMachine } from "./lib/auth";

// ============================================
// Heart Rate Analysis Functions
// ============================================

/**
 * Detect R-peaks in ECG signal using simple threshold method
 */
function detectRPeaks(ecgValues: number[], sampleRate: number): number[] {
  if (ecgValues.length < sampleRate) {
    return []; // Need at least 1 second of data
  }

  const peaks: number[] = [];

  // Calculate threshold as 60% of max value
  const maxVal = Math.max(...ecgValues);
  const minVal = Math.min(...ecgValues);
  const threshold = minVal + (maxVal - minVal) * 0.6;

  // Minimum distance between peaks (300ms = 200 BPM max)
  const minDistance = Math.floor((sampleRate * 300) / 1000);

  for (let i = 1; i < ecgValues.length - 1; i++) {
    // Local maximum above threshold
    if (
      ecgValues[i] > threshold &&
      ecgValues[i] > ecgValues[i - 1] &&
      ecgValues[i] > ecgValues[i + 1]
    ) {
      // Check minimum distance from last peak
      if (peaks.length === 0 || i - peaks[peaks.length - 1] > minDistance) {
        peaks.push(i);
      }
    }
  }

  return peaks;
}

/**
 * Calculate heart rate metrics from R-peaks
 */
function calculateHeartRateMetrics(
  peaks: number[],
  sampleRate: number,
): {
  rates: number[];
  avg: number;
  min: number;
  max: number;
  hrv: number;
} {
  if (peaks.length < 2) {
    return { rates: [], avg: 0, min: 0, max: 0, hrv: 0 };
  }

  const rates: number[] = [];
  const rrIntervals: number[] = []; // in milliseconds

  for (let i = 1; i < peaks.length; i++) {
    const rrSamples = peaks[i] - peaks[i - 1];
    const rrMs = (rrSamples / sampleRate) * 1000;
    rrIntervals.push(rrMs);

    const bpm = 60000 / rrMs;

    // Only include valid heart rates
    if (bpm >= 30 && bpm <= 220) {
      rates.push(bpm);
    }
  }

  if (rates.length === 0) {
    return { rates: [], avg: 0, min: 0, max: 0, hrv: 0 };
  }

  // Calculate metrics
  const avg = rates.reduce((a, b) => a + b, 0) / rates.length;
  const min = Math.min(...rates);
  const max = Math.max(...rates);

  // Calculate HRV using RMSSD (Root Mean Square of Successive Differences)
  let hrv = 0;
  if (rrIntervals.length >= 2) {
    let sumSquaredDiff = 0;
    for (let i = 1; i < rrIntervals.length; i++) {
      const diff = rrIntervals[i] - rrIntervals[i - 1];
      sumSquaredDiff += diff * diff;
    }
    hrv = Math.sqrt(sumSquaredDiff / (rrIntervals.length - 1));
  }

  return {
    rates,
    avg: Math.round(avg),
    min: Math.round(min),
    max: Math.round(max),
    hrv: Math.round(hrv * 10) / 10, // 1 decimal place
  };
}

// ============================================
// Internal Functions
// ============================================

/**
 * Get session info for summary generation
 */
export const getSessionForSummary = internalQuery({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("sessions"),
      startedAt: v.number(),
      endedAt: v.optional(v.number()),
      channels: v.array(v.string()),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;
    return {
      _id: session._id,
      startedAt: session.startedAt,
      endedAt: session.endedAt,
      channels: session.channels,
    };
  },
});

/**
 * Get all ECG data for summary generation
 */
export const getAllEcgDataForSummary = internalQuery({
  args: { sessionId: v.id("sessions") },
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

/**
 * Store generated summary
 */
export const storeSummary = internalMutation({
  args: {
    sessionId: v.id("sessions"),
    duration: v.number(),
    metrics: v.object({
      avgHeartRate: v.number(),
      minHeartRate: v.number(),
      maxHeartRate: v.number(),
      hrv: v.optional(v.number()),
    }),
    downsampledEcg: v.array(
      v.object({
        timestamp: v.number(),
        value: v.number(),
      }),
    ),
  },
  returns: v.id("session_summaries"),
  handler: async (ctx, args) => {
    // Check if summary already exists
    const existing = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    if (existing) {
      // Update existing summary
      await ctx.db.patch(existing._id, {
        duration: args.duration,
        metrics: args.metrics,
        downsampledEcg: args.downsampledEcg,
      });
      return existing._id;
    }

    // Create new summary
    return await ctx.db.insert("session_summaries", {
      sessionId: args.sessionId,
      duration: args.duration,
      metrics: args.metrics,
      downsampledEcg: args.downsampledEcg,
      createdAt: Date.now(),
    });
  },
});

// ============================================
// Main Summary Generation Action
// ============================================

/**
 * Generate session summary (called after session ends)
 */
export const generateSummary = internalAction({
  args: {
    sessionId: v.id("sessions"),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    console.log(`Generating summary for session ${args.sessionId}`);

    // Get session info
    const session = await ctx.runQuery(
      internal.sessionSummaries.getSessionForSummary,
      { sessionId: args.sessionId },
    );

    if (!session) {
      console.error("Session not found");
      return null;
    }

    // Get all ECG data
    const ecgData = await ctx.runQuery(
      internal.sessionSummaries.getAllEcgDataForSummary,
      { sessionId: args.sessionId },
    );

    console.log(`Found ${ecgData.length} data batches`);

    // Calculate duration
    const duration = session.endedAt
      ? Math.round((session.endedAt - session.startedAt) / 1000)
      : 0;

    // Handle empty session
    if (ecgData.length === 0) {
      console.log("No ECG data found, creating empty summary");
      await ctx.runMutation(internal.sessionSummaries.storeSummary, {
        sessionId: args.sessionId,
        duration,
        metrics: {
          avgHeartRate: 0,
          minHeartRate: 0,
          maxHeartRate: 0,
          hrv: 0,
        },
        downsampledEcg: [],
      });
      return null;
    }

    // Sort data by timestamp
    ecgData.sort((a, b) => a.timestamp - b.timestamp);

    // Extract ECG values and create downsampled version
    const sampleRate = 1000; // Assuming 1000 Hz
    let allEcgValues: number[] = [];
    const downsampledEcg: { timestamp: number; value: number }[] = [];

    for (const batch of ecgData) {
      // Find ECG channel
      const ecgChannel = batch.samples.find(
        (s) => s.channel.toUpperCase() === "ECG",
      );

      if (ecgChannel && ecgChannel.values.length > 0) {
        // Add to full array
        allEcgValues = allEcgValues.concat(ecgChannel.values);

        // Downsample: average of this batch (1 per second)
        const avgValue =
          ecgChannel.values.reduce((a, b) => a + b, 0) /
          ecgChannel.values.length;

        downsampledEcg.push({
          timestamp: batch.timestamp,
          value: Math.round(avgValue * 100) / 100, // 2 decimal places
        });
      }
    }

    console.log(`Total ECG samples: ${allEcgValues.length}`);
    console.log(`Downsampled points: ${downsampledEcg.length}`);

    // Detect R-peaks and calculate heart rate metrics
    const peaks = detectRPeaks(allEcgValues, sampleRate);
    console.log(`Detected ${peaks.length} R-peaks`);

    const hrMetrics = calculateHeartRateMetrics(peaks, sampleRate);
    console.log(
      `Heart rate: avg=${hrMetrics.avg}, min=${hrMetrics.min}, max=${hrMetrics.max}`,
    );
    console.log(`HRV (RMSSD): ${hrMetrics.hrv} ms`);

    // Store summary (limit downsampled to 3600 points = 1 hour)
    await ctx.runMutation(internal.sessionSummaries.storeSummary, {
      sessionId: args.sessionId,
      duration,
      metrics: {
        avgHeartRate: hrMetrics.avg,
        minHeartRate: hrMetrics.min,
        maxHeartRate: hrMetrics.max,
        hrv: hrMetrics.hrv,
      },
      downsampledEcg: downsampledEcg.slice(0, 3600),
    });

    console.log("Summary generation complete");
    return null;
  },
});

// ============================================
// Public Queries
// ============================================

/**
 * Get session summary
 */
export const getSummary = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("session_summaries"),
      sessionId: v.id("sessions"),
      duration: v.number(),
      metrics: v.object({
        avgHeartRate: v.number(),
        minHeartRate: v.number(),
        maxHeartRate: v.number(),
        hrv: v.optional(v.number()),
      }),
      reportFileId: v.optional(v.id("_storage")),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session for access control
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get summary
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    if (!summary) return null;

    return {
      _id: summary._id,
      sessionId: summary.sessionId,
      duration: summary.duration,
      metrics: summary.metrics,
      reportFileId: summary.reportFileId,
      createdAt: summary.createdAt,
    };
  },
});

/**
 * Get summary with downsampled ECG for chart display
 */
export const getSummaryWithEcg = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("session_summaries"),
      sessionId: v.id("sessions"),
      duration: v.number(),
      metrics: v.object({
        avgHeartRate: v.number(),
        minHeartRate: v.number(),
        maxHeartRate: v.number(),
        hrv: v.optional(v.number()),
      }),
      downsampledEcg: v.array(
        v.object({
          timestamp: v.number(),
          value: v.number(),
        }),
      ),
      reportFileId: v.optional(v.id("_storage")),
      createdAt: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const currentUser = await getCurrentUserOrThrow(ctx);

    // Get session for access control
    const session = await ctx.db.get(args.sessionId);
    if (!session) return null;

    // Check access
    const isPatient = currentUser._id === session.userId;
    const isAdmin = currentUser.role === "admin";
    const canAccessMach = await canAccessMachine(ctx, session.machineId);

    if (!isPatient && !isAdmin && !canAccessMach) {
      return null;
    }

    // Get summary with ECG data
    const summary = await ctx.db
      .query("session_summaries")
      .withIndex("by_session", (q) => q.eq("sessionId", args.sessionId))
      .unique();

    return summary;
  },
});
```

---

## Testing Steps

### Test generateSummary

1. **Create session with data**:
   - Create and start a session
   - Send 60 seconds of simulated ECG data
   - End the session

2. **Verify summary generated**:
   - Check session_summaries table
   - Verify duration matches actual duration
   - Verify heart rate values are reasonable (60-100 for normal ECG)

3. **Test empty session**:
   - Create session, start, end immediately (no data)
   - Verify summary has zero values

4. **Test long session**:
   - Send 1 hour of data (3600 batches)
   - Verify summary completes within 30 seconds
   - Verify downsampled ECG has 3600 points max

### Test Heart Rate Calculation

Use known test data:

```typescript
// 60 BPM = 1 beat per second = 1000 samples between peaks at 1000 Hz
const testEcg = generateTestEcg(60); // BPM
// Should detect peaks every 1000 samples
// HR should be ~60 BPM
```

### Test Access Control

1. Query summary as patient - verify access
2. Query as different patient - verify no access
3. Query as gestionnaire of machine - verify access
4. Query as admin - verify access

---

## Notes

- R-peak detection is a simplified algorithm - production may need more robust methods
- HRV (RMSSD) is one common metric; others include SDNN, pNN50, etc.
- Downsampling preserves overall shape but loses detail
- Consider background cleanup of old ecg_data after summary generation
- PDF report generation (Phase 5) will use the summary data
