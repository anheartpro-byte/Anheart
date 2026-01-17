# Task 1.1: Database Schema Design

## Objective

Define the complete database schema for the AnHeart ECG monitoring system with proper validators, indexes, and relationships.

## File to Create/Modify

`convex/schema.ts`

## Dependencies

None - this is the first task.

---

## Acceptance Criteria

### Users Table

- [ ] Table `users` created
- [ ] Field `clerkId` (string) - Clerk user ID
- [ ] Field `role` (union: "admin", "gestionnaire", "technician", "user")
- [ ] Field `gestionnaireId` (optional id to users) - which gestionnaire created this user
- [ ] Field `firstName` (string)
- [ ] Field `lastName` (string)
- [ ] Field `email` (string)
- [ ] Field `language` (union: "fr", "en")
- [ ] Field `createdAt` (number) - timestamp
- [ ] Index `by_clerk_id` on ["clerkId"]
- [ ] Index `by_gestionnaire` on ["gestionnaireId"]
- [ ] Index `by_role` on ["role"]

### Machines Table

- [ ] Table `machines` created
- [ ] Field `name` (string)
- [ ] Field `apiKey` (string) - hashed API key
- [ ] Field `gestionnaireId` (id to users) - owner
- [ ] Field `status` (union: "online", "offline", "in_session")
- [ ] Field `lastHeartbeat` (number) - timestamp
- [ ] Field `location` (optional string)
- [ ] Field `config` (object with sampleRate, channels, batchInterval)
- [ ] Field `createdAt` (number)
- [ ] Index `by_gestionnaire` on ["gestionnaireId"]
- [ ] Index `by_api_key` on ["apiKey"]
- [ ] Index `by_status` on ["status"]

### Sessions Table

- [ ] Table `sessions` created
- [ ] Field `machineId` (id to machines)
- [ ] Field `userId` (id to users) - patient
- [ ] Field `technicianId` (optional id to users)
- [ ] Field `status` (union: "pending", "active", "completed", "failed")
- [ ] Field `startedAt` (number)
- [ ] Field `endedAt` (optional number)
- [ ] Field `channels` (array of strings)
- [ ] Field `notes` (optional string)
- [ ] Index `by_user` on ["userId"]
- [ ] Index `by_machine` on ["machineId"]
- [ ] Index `by_machine_and_status` on ["machineId", "status"]
- [ ] Index `by_technician` on ["technicianId"]

### ECG Data Table

- [ ] Table `ecg_data` created
- [ ] Field `sessionId` (id to sessions)
- [ ] Field `timestamp` (number)
- [ ] Field `samples` (array of objects with channel and values)
- [ ] Index `by_session_and_timestamp` on ["sessionId", "timestamp"]

### Session Summaries Table

- [ ] Table `session_summaries` created
- [ ] Field `sessionId` (id to sessions)
- [ ] Field `duration` (number) - seconds
- [ ] Field `metrics` (object with avgHeartRate, minHeartRate, maxHeartRate, hrv)
- [ ] Field `downsampledEcg` (array of timestamp/value objects)
- [ ] Field `reportFileId` (optional id to \_storage)
- [ ] Field `createdAt` (number)
- [ ] Index `by_session` on ["sessionId"]

### Machine Heartbeats Table

- [ ] Table `machine_heartbeats` created
- [ ] Field `machineId` (id to machines)
- [ ] Field `timestamp` (number)
- [ ] Field `batteryLevel` (optional number)
- [ ] Field `wifiStrength` (optional number)
- [ ] Field `activeSessionId` (optional id to sessions)
- [ ] Index `by_machine_and_timestamp` on ["machineId", "timestamp"]

### General

- [ ] Schema compiles without errors (`npx convex dev`)
- [ ] All 6 tables visible in Convex dashboard
- [ ] Can insert test document in each table via dashboard

---

## Implementation

```typescript
// convex/schema.ts
import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  users: defineTable({
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
  })
    .index("by_clerk_id", ["clerkId"])
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_role", ["role"]),

  machines: defineTable({
    name: v.string(),
    apiKey: v.string(),
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
  })
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_api_key", ["apiKey"])
    .index("by_status", ["status"]),

  sessions: defineTable({
    machineId: v.id("machines"),
    userId: v.id("users"),
    technicianId: v.optional(v.id("users")),
    status: v.union(
      v.literal("pending"),
      v.literal("active"),
      v.literal("completed"),
      v.literal("failed"),
    ),
    startedAt: v.number(),
    endedAt: v.optional(v.number()),
    channels: v.array(v.string()),
    notes: v.optional(v.string()),
  })
    .index("by_user", ["userId"])
    .index("by_machine", ["machineId"])
    .index("by_machine_and_status", ["machineId", "status"])
    .index("by_technician", ["technicianId"]),

  ecg_data: defineTable({
    sessionId: v.id("sessions"),
    timestamp: v.number(),
    samples: v.array(
      v.object({
        channel: v.string(),
        values: v.array(v.number()),
      }),
    ),
  }).index("by_session_and_timestamp", ["sessionId", "timestamp"]),

  session_summaries: defineTable({
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
  }).index("by_session", ["sessionId"]),

  machine_heartbeats: defineTable({
    machineId: v.id("machines"),
    timestamp: v.number(),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.id("sessions")),
  }).index("by_machine_and_timestamp", ["machineId", "timestamp"]),
});
```

---

## Testing Steps

1. Run `npx convex dev` and verify no compilation errors
2. Open Convex dashboard (`npx convex dashboard`)
3. Navigate to Data tab
4. Verify all 6 tables appear: users, machines, sessions, ecg_data, session_summaries, machine_heartbeats
5. Try inserting a test document in `users` table with all required fields
6. Verify the document is created successfully
7. Delete the test document

---

## Notes

- System fields `_id` and `_creationTime` are automatically added by Convex
- All indexes must be queried in the order defined
- The `numbers` table from the starter template can be deleted
