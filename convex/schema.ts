import { defineSchema, defineTable } from "convex/server";
import { v } from "convex/values";

/**
 * What the machine is doing right now, as the Pi's local panel reports it with
 * each heartbeat. Values are copied from the Pi's TelemetrySnapshot; `bpm` is
 * absent whenever the Pi has no fresh, trustworthy heart rate (never a stale
 * number shown as live).
 */
export const liveStateValidator = v.object({
  runMode: v.string(), // "repos" | "manuel" | "seance" | "arret"
  phase: v.string(), // Phase wire value, e.g. "baseline", "warmup", "hold"
  bpm: v.optional(v.number()),
  motorRpm: v.number(), // measured, motor shaft
  outputRpm: v.number(), // measured, arm (motor / 49.79)
  setpointMotorRpm: v.number(),
  gLoad: v.number(), // resultant g at the configured radius
  safetyAction: v.string(), // "none" | "hold" | "reduce" | "ramp_down" | ...
  driveState: v.optional(v.string()),
  sessionId: v.optional(v.string()),
  updatedAt: v.number(),
});

/** One training preset as synced from the Pi's ProfileStore (read-only here). */
export const machineProfileFields = {
  profileId: v.string(),
  name: v.string(),
  totalDurationS: v.number(),
  zoneLowBpm: v.number(),
  zoneHighBpm: v.number(),
  hardMaxBpm: v.number(),
  criticalBpm: v.number(),
  subjectHrMax: v.number(),
  minRunRpm: v.number(),
  maxRpm: v.number(),
};

/**
 * The two kinds of training session. A session of the retired ECG recording
 * mode has no `kind` at all (that mode never wrote one): it is read-only
 * history, reported as "recording" by the queries that list sessions.
 */
export const sessionKindValidator = v.union(
  v.literal("auto"), // pre-saved programme, HR-controlled; remote or local
  v.literal("manual"), // operator-set speed; ONLY ever started at the machine
);

/** A separately versioned part of the product (ANH-134). */
export const softwareComponentValidator = v.union(
  v.literal("pi"), // Raspberry Pi client, tags pi-X.Y.Z
  v.literal("cloud"), // Convex backend, tags cloud-X.Y.Z
  v.literal("web"), // site, tags web-X.Y.Z
);

/** How far a Pi version has been validated, lowest first (ANH-134). */
export const validationLevelValidator = v.union(
  v.literal("bench"), // bench, empty capsule (M3)
  v.literal("auto_validated"), // programmed sessions (M5)
  v.literal("occupied_validated"), // a person on board (M6)
);

export default defineSchema({
  // Users - Extended Clerk user data with roles and relationships
  users: defineTable({
    clerkId: v.string(),
    role: v.union(
      v.literal("admin"),
      v.literal("gestionnaire"),
      v.literal("user"),
    ),
    // Legacy field - kept for migration compatibility
    gestionnaireId: v.optional(v.id("users")),
    firstName: v.string(),
    lastName: v.string(),
    email: v.string(),
    language: v.union(v.literal("fr"), v.literal("en")),
    // Physiology used to vet a training zone before a remote launch. A measured
    // maximum always wins over the age estimate (Tanaka: 208 - 0.7 x age).
    hrMax: v.optional(v.number()),
    birthYear: v.optional(v.number()),
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

  // Launch rights: which users may launch AUTO training sessions on which
  // machine. Granted and revoked only by an admin or a gestionnaire managing
  // both the machine and the user. Manual sessions are never remote.
  machine_user_permissions: defineTable({
    machineId: v.id("machines"),
    userId: v.id("users"),
    grantedBy: v.id("users"),
    createdAt: v.number(),
  })
    .index("by_machine", ["machineId"])
    .index("by_user", ["userId"])
    .index("by_machine_and_user", ["machineId", "userId"]),

  // Released software versions (ANH-134), one row per component version,
  // written only by an admin (`softwareReleases.recordRelease`). The rule that
  // reads `validationLevel` is `lib/releaseValidation.ts`: a machine validated
  // for programmed sessions only receives a Pi version that is
  // `auto_validated` or higher, and a machine validated for a person on board
  // only an `occupied_validated` one.
  software_releases: defineTable({
    component: softwareComponentValidator,
    version: v.string(), // the tag of the version, e.g. "pi-0.1.0"
    validationLevel: v.optional(validationLevelValidator), // Pi versions only
    releasedAt: v.number(),
    notes: v.optional(v.string()),
    recordedBy: v.id("users"),
    updatedAt: v.number(),
  }).index("by_component_and_version", ["component", "version"]),

  // Training presets, mirrored from each Pi. The Pi is the authority; this
  // table is replaced wholesale on every sync.
  machine_profiles: defineTable({
    machineId: v.id("machines"),
    ...machineProfileFields,
    storeRev: v.number(),
    updatedAt: v.number(),
  })
    .index("by_machine", ["machineId"])
    .index("by_machine_and_profile", ["machineId", "profileId"]),

  // Machines - Raspberry Pi devices
  machines: defineTable({
    name: v.string(),
    apiKey: v.string(), // Versioned salted digest; legacy values require replacement.
    apiKeySelector: v.optional(v.string()),
    authenticationEnabled: v.optional(v.boolean()),
    status: v.union(
      v.literal("online"),
      v.literal("offline"),
      v.literal("in_session"),
    ),
    lastHeartbeat: v.number(),
    location: v.optional(v.string()),
    // DEPRECATED: settings of the retired ECG recorder. No function reads or
    // writes this field. It stays declared, optional, only so that a
    // deployment whose machines still carry it accepts this schema; the
    // migration `migrations/retireLegacyRecording:removeMachineConfig` strips
    // it from the documents, after which the field can leave the schema.
    config: v.optional(
      v.object({
        sampleRate: v.number(),
        channels: v.array(v.string()),
        batchInterval: v.number(),
      }),
    ),
    createdAt: v.number(),
    isDeleted: v.optional(v.boolean()), // Soft delete flag
    deletedAt: v.optional(v.number()), // When it was deleted
    deletedBy: v.optional(v.id("users")), // Who deleted it
    // Reported by the Pi: whether this build accepts programmed (auto) sessions.
    programsEnabled: v.optional(v.boolean()),
    live: v.optional(liveStateValidator),
  })
    .index("by_api_key", ["apiKey"])
    .index("by_apiKeySelector", ["apiKeySelector"])
    .index("by_status", ["status"])
    .index("by_is_deleted", ["isDeleted"]),

  // Sessions - training sessions (auto, manual), plus the read-only history
  // of the retired ECG recording mode (rows without `kind`).
  sessions: defineTable({
    machineId: v.id("machines"),
    // The rider. Optional only for sessions started at the machine with no
    // rider chosen from the synced roster; every remote launch sets it.
    userId: v.optional(v.id("users")),
    startedById: v.optional(v.id("users")), // Who started the session
    status: v.union(
      v.literal("pending"),
      v.literal("active"),
      v.literal("completed"),
      v.literal("failed"),
    ),
    startedAt: v.number(),
    endedAt: v.optional(v.number()),
    channels: v.array(v.string()), // ["ECG"] on a training session
    sampleRate: v.optional(v.number()), // Hz - history of the recording mode; no longer written
    notes: v.optional(v.string()),
    // --- training (absent on the sessions of the retired recording mode) ---
    kind: v.optional(sessionKindValidator),
    origin: v.optional(v.union(v.literal("remote"), v.literal("local"))),
    profileId: v.optional(v.string()),
    profileName: v.optional(v.string()),
    zoneLowBpm: v.optional(v.number()),
    zoneHighBpm: v.optional(v.number()),
    totalDurationS: v.optional(v.number()),
    subjectHrMax: v.optional(v.number()),
    subjectAge: v.optional(v.number()),
    subjectLabel: v.optional(v.string()),
    operatorName: v.optional(v.string()),
    localRef: v.optional(v.string()), // Pi-side idempotency key, local sessions
    stopRequestedAt: v.optional(v.number()),
    endReason: v.optional(v.string()),
  })
    .index("by_user", ["userId"])
    .index("by_machine_and_local_ref", ["machineId", "localRef"])
    .index("by_machine", ["machineId"])
    .index("by_machine_and_status", ["machineId", "status"])
    .index("by_started_by", ["startedById"]),

  // ECG Data - HISTORY of the retired ECG recording mode. Read-only: no
  // function writes to this table any more.
  // The recorder filtered, converted to physical units, downsampled and
  // computed metrics before sending, so `values` are treated (e.g. mV) at
  // `sampleRate` Hz (default 250), not raw ADC.
  ecg_data: defineTable({
    sessionId: v.id("sessions"),
    timestamp: v.number(),
    sampleRate: v.optional(v.number()), // treated/transmitted rate in Hz (e.g. 250)
    samples: v.array(
      v.object({
        channel: v.string(),
        values: v.array(v.number()), // ~1 second of treated data
        unit: v.optional(v.string()), // physical unit of values, e.g. "mV"
      }),
    ),
    // Per-channel clinical metrics computed on-device (BioSPPy).
    metrics: v.optional(
      v.record(
        v.string(),
        v.object({
          heartRate: v.optional(v.number()),
          hrv: v.optional(v.number()),
          respRate: v.optional(v.number()),
          scrCount: v.optional(v.number()),
          activations: v.optional(v.number()),
          pulse: v.optional(v.number()),
          quality: v.optional(v.string()),
        }),
      ),
    ),
  }).index("by_session_and_timestamp", ["sessionId", "timestamp"]),

  // Session Summaries - HISTORY of the retired ECG recording mode. Read-only:
  // nothing computes or stores a summary any more.
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

  // Training telemetry, ~1 Hz, from the Pi's TelemetrySnapshot.
  training_telemetry: defineTable({
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    t: v.number(), // unix ms, Pi clock
    elapsedS: v.number(),
    phase: v.string(),
    bpm: v.optional(v.number()), // absent = no fresh trustworthy HR
    motorRpm: v.number(),
    outputRpm: v.number(),
    setpointMotorRpm: v.number(),
    gLoad: v.number(),
    safetyAction: v.string(),
  }).index("by_session_and_t", ["sessionId", "t"]),

  // Machine Heartbeats - For monitoring (can be cleaned up periodically)
  machine_heartbeats: defineTable({
    machineId: v.id("machines"),
    timestamp: v.number(),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.id("sessions")),
  }).index("by_machine_and_timestamp", ["machineId", "timestamp"]),
});
