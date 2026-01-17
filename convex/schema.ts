import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

export default defineSchema({
  // Users - Extended Clerk user data with roles and relationships
  users: defineTable({
    clerkId: v.string(),
    role: v.union(
      v.literal("admin"),
      v.literal("gestionnaire"),
      v.literal("technician"),
      v.literal("user"),
    ),
    // For technicians: which gestionnaire they work for (single relationship)
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

  // User-Gestionnaire relationship (many-to-many for patients)
  // A user (patient) can be managed by multiple gestionnaires
  // A gestionnaire can manage multiple users (patients)
  user_gestionnaires: defineTable({
    userId: v.id("users"), // The patient
    gestionnaireId: v.id("users"), // The gestionnaire managing this patient
    createdAt: v.number(),
    createdBy: v.id("users"), // Who created this relationship
  })
    .index("by_user", ["userId"])
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_user_and_gestionnaire", ["userId", "gestionnaireId"]),

  // Machine-Gestionnaire relationship (many-to-many)
  // A machine can be managed by multiple gestionnaires
  // A gestionnaire can manage multiple machines
  machine_gestionnaires: defineTable({
    machineId: v.id("machines"),
    gestionnaireId: v.id("users"),
    isOwner: v.boolean(), // The original creator/owner of the machine
    createdAt: v.number(),
    createdBy: v.id("users"), // Who created this relationship
  })
    .index("by_machine", ["machineId"])
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_machine_and_gestionnaire", ["machineId", "gestionnaireId"]),

  // Machines - Raspberry Pi devices
  machines: defineTable({
    name: v.string(),
    apiKey: v.string(), // Hashed API key
    status: v.union(
      v.literal("online"),
      v.literal("offline"),
      v.literal("in_session"),
    ),
    lastHeartbeat: v.number(),
    location: v.optional(v.string()),
    config: v.object({
      sampleRate: v.number(), // Hz (default 1000)
      channels: v.array(v.string()), // ["ECG", "EMG", etc.]
      batchInterval: v.number(), // ms (default 1000)
    }),
    createdAt: v.number(),
  })
    .index("by_api_key", ["apiKey"])
    .index("by_status", ["status"]),

  // Sessions - ECG recording sessions
  sessions: defineTable({
    machineId: v.id("machines"),
    userId: v.id("users"), // Patient
    technicianId: v.optional(v.id("users")), // Who started it
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

  // ECG Data - Real-time streaming data batches
  ecg_data: defineTable({
    sessionId: v.id("sessions"),
    timestamp: v.number(),
    samples: v.array(
      v.object({
        channel: v.string(),
        values: v.array(v.number()), // 1 second of data (~1000 samples at 1kHz)
      }),
    ),
  }).index("by_session_and_timestamp", ["sessionId", "timestamp"]),

  // Session Summaries - Computed after session ends
  session_summaries: defineTable({
    sessionId: v.id("sessions"),
    duration: v.number(), // seconds
    metrics: v.object({
      avgHeartRate: v.number(),
      minHeartRate: v.number(),
      maxHeartRate: v.number(),
      hrv: v.optional(v.number()), // Heart rate variability (RMSSD)
    }),
    downsampledEcg: v.array(
      v.object({
        timestamp: v.number(),
        value: v.number(),
      }),
    ),
    reportFileId: v.optional(v.id("_storage")), // PDF report
    createdAt: v.number(),
  }).index("by_session", ["sessionId"]),

  // Machine Heartbeats - For monitoring (can be cleaned up periodically)
  machine_heartbeats: defineTable({
    machineId: v.id("machines"),
    timestamp: v.number(),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.id("sessions")),
  }).index("by_machine_and_timestamp", ["machineId", "timestamp"]),
});
