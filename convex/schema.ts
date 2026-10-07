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
/**
 * Role of a member inside ONE organisation: the mirror of the Clerk roles
 * `org:admin`, `org:gestionnaire` and `org:patient`. Whether an `admin` is the
 * Anheart super-admin is never stored: `convex/lib/auth.ts` derives it from the
 * organisation the role is held in.
 */
export const organizationRoleValidator = v.union(
  v.literal("admin"),
  v.literal("gestionnaire"),
  v.literal("user"),
);

/**
 * The organisation (client) a row belongs to. Optional only so that rows
 * written before the multi-organisation migration still load: a row without
 * it is served to no one but the Anheart admin (`convex/lib/auth.ts`).
 */
const organizationId = v.optional(v.id("organizations"));

export default defineSchema({
  // Organisations (clients) - mirror of Clerk Organizations. Clerk carries
  // membership, roles and invitations; Convex stays the authority on the data.
  organizations: defineTable({
    // Absent only on the default organisation created by the migration, until
    // it is linked to the Clerk organisation named by ANHEART_ORG_ID.
    clerkOrgId: v.optional(v.string()),
    name: v.string(),
    slug: v.string(),
    createdAt: v.number(),
    settings: v.object({
      requirePrescription: v.boolean(),
      language: v.union(v.literal("fr"), v.literal("en")),
    }),
  })
    .index("by_clerk_org_id", ["clerkOrgId"])
    .index("by_slug", ["slug"]),

  // Memberships - mirror of Clerk organisation memberships. A user may belong
  // to several organisations; `active: false` keeps the trace of a removal.
  memberships: defineTable({
    userId: v.id("users"),
    organizationId: v.id("organizations"),
    role: organizationRoleValidator,
    active: v.boolean(),
  })
    .index("by_user", ["userId"])
    .index("by_organization", ["organizationId"])
    .index("by_user_and_organization", ["userId", "organizationId"]),

  // Users - Extended Clerk user data with roles and relationships
  users: defineTable({
    clerkId: v.string(),
    // Read-only mirror of the role held in the organisation below. It is
    // never read to authorise the caller: see `convex/lib/auth.ts`.
    role: organizationRoleValidator,
    // Main organisation of the account.
    organizationId,
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
    .index("by_role", ["role"])
    .index("by_organization", ["organizationId"]),

  // User-Gestionnaire relationship (many-to-many for patients)
  // A user (patient) can be managed by multiple gestionnaires
  // A gestionnaire can manage multiple users (patients)
  user_gestionnaires: defineTable({
    organizationId, // The organisation the link lives in
    userId: v.id("users"), // The patient
    gestionnaireId: v.id("users"), // The gestionnaire managing this patient
    createdAt: v.number(),
    createdBy: v.id("users"), // Who created this relationship
  })
    .index("by_user", ["userId"])
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_user_and_gestionnaire", ["userId", "gestionnaireId"])
    .index("by_organization", ["organizationId"]),

  // Machine-Gestionnaire relationship (many-to-many)
  // A machine can be managed by multiple gestionnaires
  // A gestionnaire can manage multiple machines
  machine_gestionnaires: defineTable({
    organizationId, // Always the machine's organisation
    machineId: v.id("machines"),
    gestionnaireId: v.id("users"),
    isOwner: v.boolean(), // The original creator/owner of the machine
    createdAt: v.number(),
    createdBy: v.id("users"), // Who created this relationship
  })
    .index("by_machine", ["machineId"])
    .index("by_gestionnaire", ["gestionnaireId"])
    .index("by_machine_and_gestionnaire", ["machineId", "gestionnaireId"])
    .index("by_organization", ["organizationId"]),

  // Launch rights: which users may launch AUTO training sessions on which
  // machine. Granted and revoked only by an admin or a gestionnaire managing
  // both the machine and the user. Manual sessions are never remote.
  machine_user_permissions: defineTable({
    organizationId, // Always the machine's organisation
    machineId: v.id("machines"),
    userId: v.id("users"),
    grantedBy: v.id("users"),
    createdAt: v.number(),
  })
    .index("by_machine", ["machineId"])
    .index("by_user", ["userId"])
    .index("by_machine_and_user", ["machineId", "userId"])
    .index("by_organization", ["organizationId"]),

  // Released software versions (ANH-134), one row per component version,
  // written only by an admin (`softwareReleases.recordRelease`). The rule that
  // reads `validationLevel` is `lib/releaseValidation.ts`: a machine validated
  // for programmed sessions only receives a Pi version that is
  // `auto_validated` or higher, and a machine validated for a person on board
  // only an `occupied_validated` one.
  //
  // Anheart-wide on purpose: no `organizationId`. A released version is the
  // same for every organisation, and only the Anheart admin reads or writes
  // the register (`convex/softwareReleases.ts`).
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
    organizationId, // Always the machine's organisation
    machineId: v.id("machines"),
    ...machineProfileFields,
    storeRev: v.number(),
    updatedAt: v.number(),
  })
    .index("by_machine", ["machineId"])
    .index("by_machine_and_profile", ["machineId", "profileId"])
    .index("by_organization", ["organizationId"]),

  // Machines - Raspberry Pi devices. A machine belongs to exactly one
  // organisation; everything it writes through the machine routes inherits
  // that organisation on the server.
  machines: defineTable({
    organizationId,
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
    // Announced by the Pi with each heartbeat (contracts/machine-api.json):
    // its software version (a git tag, e.g. "pi-0.4.2"; absent when it sent
    // none), the contract it speaks, and when both were last seen.
    softwareVersion: v.optional(v.string()),
    contractVersion: v.optional(v.string()),
    lastVersionSeenAt: v.optional(v.number()),
  })
    .index("by_api_key", ["apiKey"])
    .index("by_apiKeySelector", ["apiKeySelector"])
    .index("by_status", ["status"])
    .index("by_is_deleted", ["isDeleted"])
    .index("by_organization", ["organizationId"]),

  // Sessions - training sessions (auto, manual), plus the read-only history
  // of the retired ECG recording mode (rows without `kind`).
  sessions: defineTable({
    organizationId, // Always the machine's organisation
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
    // The start as the machine dated it, on its own clock (unix ms). The
    // telemetry and the events of the session are dated on that same clock:
    // with `startedAt`, which the server dates, it is what places them on the
    // server's clock (`convex/training.ts`, `machineAxis`). Absent on a
    // session whose machine never said it.
    machineStartedAt: v.optional(v.number()),
    stopRequestedAt: v.optional(v.number()),
    endReason: v.optional(v.string()),
  })
    .index("by_user", ["userId"])
    .index("by_machine_and_local_ref", ["machineId", "localRef"])
    .index("by_machine", ["machineId"])
    .index("by_machine_and_status", ["machineId", "status"])
    .index("by_started_by", ["startedById"])
    .index("by_organization", ["organizationId"]),

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

  // Training telemetry, ~1 Hz, as the Pi reads it back from the session's
  // local record. One row per (sessionId, t): a point sent twice is stored
  // once (`storeTelemetry`).
  training_telemetry: defineTable({
    organizationId, // Always the session's organisation
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    t: v.number(), // unix ms on the machine's clock: the session's start as it dated it, plus the time elapsed
    elapsedS: v.number(),
    phase: v.string(),
    bpm: v.optional(v.number()), // absent = no fresh trustworthy HR
    motorRpm: v.number(),
    outputRpm: v.number(),
    setpointMotorRpm: v.number(),
    gLoad: v.number(),
    safetyAction: v.string(),
  })
    .index("by_session_and_t", ["sessionId", "t"])
    .index("by_organization", ["organizationId"]),

  // Events of a training session (verdicts, refusals, phases, drive faults,
  // remote commands, preflight, warnings, the end), as the Pi reads them back
  // from the session's local record. `seq` is the rank of the event in that
  // record: one row per (sessionId, seq), however many times the machine
  // sends it (`storeEvents`).
  training_events: defineTable({
    organizationId, // Always the session's organisation
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    seq: v.number(),
    t: v.number(), // unix ms on the machine's clock, like `training_telemetry.t`
    kind: v.string(),
    detail: v.string(),
    actor: v.string(), // "system", "remote" or an opaque operator identifier
  })
    .index("by_session_and_seq", ["sessionId", "seq"])
    .index("by_organization", ["organizationId"]),

  // Machine Heartbeats - For monitoring (can be cleaned up periodically)
  machine_heartbeats: defineTable({
    machineId: v.id("machines"),
    timestamp: v.number(),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.id("sessions")),
  }).index("by_machine_and_timestamp", ["machineId", "timestamp"]),
});
