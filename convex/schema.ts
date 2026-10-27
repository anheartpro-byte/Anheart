import { defineSchema, defineTable } from "convex/server";
import {
  auditActionValidator,
  auditActorRoleValidator,
  auditDetailsValidator,
  auditSourceValidator,
  auditTargetTypeValidator,
} from "./lib/audit";
import {
  feedbackComfortValidator,
  feedbackSubmitterValidator,
} from "./lib/feedback";
import { recordState } from "./lib/recordStorage";
import {
  summaryFiguresValidator,
  summarySourceValidator,
} from "./lib/sessionSummary";
import { v } from "convex/values";
import { MACHINE_VALIDATION_LEVELS } from "./lib/releaseValidation";
import {
  machineHealthValidator,
  healthMeasurementsValidator,
} from "./lib/machineHealth";

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
  // Centripetal g at the reference radius, not the resultant felt with
  // gravity (`SpeedView.g_load`, raspberry-pi/src/training/types.py).
  gLoad: v.number(),
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

export const machineValidationLevelValidator = v.union(
  ...MACHINE_VALIDATION_LEVELS.map((level) => v.literal(level)),
);
export const machineReviewLevelValidator = v.union(
  v.literal("bench"),
  v.literal("auto"),
  v.literal("occupied"),
);
export const driveParameterSheetValidator = v.object({
  storageId: v.id("_storage"),
  contentType: v.union(
    v.literal("application/pdf"),
    v.literal("application/json"),
  ),
  version: v.string(),
  signedBy: v.string(),
  at: v.number(),
  attachedBy: v.id("users"),
  attachedAt: v.number(),
});
export const machineValidationValidator = v.object({
  level: machineValidationLevelValidator,
  reviews: v.array(
    v.object({
      level: machineReviewLevelValidator,
      by: v.id("users"),
      at: v.number(),
      reportStorageId: v.id("_storage"),
      softwareVersion: v.string(),
      medicalParametersVersion: v.union(v.string(), v.null()),
    }),
  ),
  downgrades: v.array(
    v.object({
      from: machineValidationLevelValidator,
      to: machineValidationLevelValidator,
      by: v.id("users"),
      at: v.number(),
      reason: v.string(),
    }),
  ),
});
export const machineRegistryIdentityFields = {
  serialPi: v.optional(v.string()),
  serialDrive: v.optional(v.string()),
  serialMotor: v.optional(v.string()),
  serialGearbox: v.optional(v.string()),
  bitalinoAddress: v.optional(v.string()),
  commissionedAt: v.optional(v.number()),
  site: v.optional(v.string()),
  expectedSoftwareVersion: v.optional(v.string()),
};
export const machineRegistryRadiiFields = {
  armRadiusM: v.optional(v.number()),
  legTipRadiusM: v.optional(v.number()),
  radiiMeasuredAt: v.optional(v.number()),
  radiiMeasuredBy: v.optional(v.string()),
};

/**
 * What an alert to the gestionnaires is about (ANH-90). Another kind (the
 * incidents of ANH-145) is one more literal here and in
 * `notificationEmailFields`, a field of `emailChoicesValidator`
 * (convex/notifications.ts), an entry of `NOTIFICATION_TYPES`
 * (convex/lib/notifications.ts), a type in `TYPES` on the alerts page
 * (app/[locale]/dashboard/notifications/page.tsx), French and English labels
 * in `alertEmail` (convex/lib/notificationEmail.ts), and its translations.
 */
export const notificationTypeValidator = v.union(
  v.literal("machine_offline"), // no heartbeat for LIVE_FRESH_MS
  v.literal("session_failed"), // the machine ended a session as failed
  v.literal("drive_fault"), // the drive reports a fault
  v.literal("safety_verdict"), // a safety verdict stopped a session
);

/** Whether each kind of alert is sent by e-mail too; unset: its default. */
export const notificationEmailFields = {
  machine_offline: v.optional(v.boolean()),
  session_failed: v.optional(v.boolean()),
  drive_fault: v.optional(v.boolean()),
  safety_verdict: v.optional(v.boolean()),
};
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
  // Append-only, written only by audit() in convex/lib/audit.ts. No function
  // modifies or deletes a row, and there is no purge. Retention of five years
  // remains to be confirmed by ANH-87a. A row without an organisation describes
  // a global action and is readable only by an Anheart admin (auth.inScope).
  audit_log: defineTable({
    at: v.number(), // Unix milliseconds at insertion
    organizationId: v.optional(v.id("organizations")),
    actorId: v.optional(v.union(v.id("users"), v.id("machines"))),
    actorRole: auditActorRoleValidator,
    action: auditActionValidator,
    targetType: auditTargetTypeValidator,
    targetId: v.string(),
    details: v.optional(auditDetailsValidator),
    requestId: v.string(),
    source: auditSourceValidator, // Includes operator CLI calls as an EX-1 extension.
  })
    .index("by_at", ["at"])
    .index("by_organization_and_at", ["organizationId", "at"])
    .index("by_actor_and_at", ["actorId", "at"])
    .index("by_actor_role_and_at", ["actorRole", "at"])
    .index("by_organization_and_actor_role_and_at", [
      "organizationId",
      "actorRole",
      "at",
    ])
    .index("by_action_and_at", ["action", "at"])
    .index("by_target_and_at", ["targetType", "targetId", "at"])
    .index("by_target_type_and_at", ["targetType", "at"])
    .index("by_organization_and_target_type_and_at", [
      "organizationId",
      "targetType",
      "at",
    ])
    .index("by_organization_and_actor_and_at", [
      "organizationId",
      "actorId",
      "at",
    ])
    .index("by_organization_and_action_and_at", [
      "organizationId",
      "action",
      "at",
    ])
    .index("by_organization_and_target_and_at", [
      "organizationId",
      "targetType",
      "targetId",
      "at",
    ]),

  // Organisations (clients) - mirror of Clerk Organizations. Clerk carries
  // membership, roles and invitations; Convex stays the authority on the data.
  organizations: defineTable({
    // Absent only on the default organisation created by the migration, until
    // it is linked to the Clerk organisation named by ANHEART_ORG_ID.
    clerkOrgId: v.optional(v.string()),
    clerkUpdatedAt: v.optional(v.number()),
    clerkEventAt: v.optional(v.number()),
    deletedAt: v.optional(v.number()),
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
    clerkMembershipId: v.optional(v.string()),
    clerkUpdatedAt: v.optional(v.number()),
    clerkEventAt: v.optional(v.number()),
    userId: v.id("users"),
    organizationId: v.id("organizations"),
    role: organizationRoleValidator,
    active: v.boolean(),
  })
    .index("by_user", ["userId"])
    .index("by_organization", ["organizationId"])
    .index("by_user_and_organization", ["userId", "organizationId"])
    // The active members of one organisation, or of all of them: what the
    // dashboard counts (`dashboard.counters`) without reading a removal.
    .index("by_active_and_organization", ["active", "organizationId"]),

  clerk_webhook_events: defineTable({
    eventId: v.string(),
    receivedAt: v.number(),
  }).index("by_event_id", ["eventId"]),

  patient_invitations: defineTable({
    organizationId: v.id("organizations"),
    userId: v.optional(v.id("users")),
    email: v.string(),
    requestedBy: v.id("users"),
    gestionnaireIds: v.array(v.id("users")),
    status: v.union(
      v.literal("pending"),
      v.literal("sent"),
      v.literal("accepted"),
      v.literal("failed"),
    ),
    clerkInvitationId: v.optional(v.string()),
    createdAt: v.number(),
    updatedAt: v.number(),
  }).index("by_organization_and_email", ["organizationId", "email"]),

  // Users - Extended Clerk user data with roles and relationships
  users: defineTable({
    clerkId: v.string(),
    clerkDeletedAt: v.optional(v.number()),
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

  // Passenger fitness answers are health data: only the passenger's managers
  // and admins may read them through convex/fitness.ts.
  fitness_assessments: defineTable({
    userId: v.id("users"),
    organizationId: v.id("organizations"),
    questionnaireVersion: v.string(),
    answers: v.record(v.string(), v.boolean()),
    outcome: v.union(
      v.literal("fit"),
      v.literal("unfit"),
      v.literal("needs_medical_opinion"),
    ),
    submittedBy: v.id("users"),
    submittedAt: v.number(),
    validatedBy: v.optional(v.id("users")),
    validatedAt: v.optional(v.number()),
    validUntil: v.optional(v.number()),
    notes: v.optional(v.string()),
  }).index("by_user_and_organization", [
    "userId",
    "organizationId",
    "submittedAt",
  ]),

  consents: defineTable({
    userId: v.id("users"),
    organizationId: v.id("organizations"),
    textVersion: v.string(),
    signedAt: v.number(),
    signedVia: v.union(v.literal("site"), v.literal("console")),
    withdrawnAt: v.optional(v.number()),
    withdrawnBy: v.optional(v.id("users")),
  }).index("by_user_and_organization", [
    "userId",
    "organizationId",
    "signedAt",
  ]),

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

  // Every validation level a Pi version has held, one row per decision,
  // oldest first, never rewritten: who decided it, when and why (ANH-195).
  // `software_releases` says which level is in force; this table says how the
  // version got there. Anheart-wide like the register: no `organizationId`.
  software_release_levels: defineTable({
    releaseId: v.id("software_releases"),
    validationLevel: validationLevelValidator,
    reason: v.optional(v.string()), // the notes given with the decision
    decidedBy: v.id("users"),
    decidedAt: v.number(), // unix ms
  }).index("by_release", ["releaseId"]),

  medical_parameters_versions: defineTable({
    version: v.string(),
    issuedAt: v.string(),
    storageId: v.id("_storage"),
    signedBy: v.string(),
    signature: v.string(),
    zoneCeilingFraction: v.number(),
    minRiderAge: v.number(),
    hrMaxFormula: v.literal("tanaka"),
    registeredAt: v.number(),
  }).index("by_version", ["version"]),

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
    // The single key of a machine written before ANH-165: its verifier and its
    // public selector. The keys now live in `machine_api_keys`; the migration
    // `migrations/machineApiKeys:moveMachineKeysToTable` moves these two
    // fields there and clears them. Until it has run, a key still found here
    // opens the machine routes (`convex/lib/machineAuth.ts`).
    apiKey: v.optional(v.string()),
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
    medicalParametersVersion: v.optional(v.union(v.string(), v.null())),
    health: v.optional(machineHealthValidator),
    healthUpdatedAt: v.optional(v.number()),
  })
    .index("by_api_key", ["apiKey"])
    .index("by_apiKeySelector", ["apiKeySelector"])
    .index("by_status", ["status"])
    .index("by_is_deleted", ["isDeleted"])
    .index("by_organization", ["organizationId"]),

  // Hardware identity, measured geometry and validation decisions (ANH-147).
  // One row per machine, written only by Anheart admins. Missing row: level
  // none. Organisation follows the machine on every write, like profiles.
  machine_registry: defineTable({
    organizationId,
    machineId: v.id("machines"),
    ...machineRegistryIdentityFields,
    ...machineRegistryRadiiFields,
    contacts: v.array(v.id("users")),
    driveParameterSheet: v.optional(driveParameterSheetValidator),
    driveParameterSheetHistory: v.array(driveParameterSheetValidator),
    validation: machineValidationValidator,
    updatedAt: v.number(),
    updatedBy: v.id("users"),
  })
    .index("by_machine", ["machineId"])
    .index("by_organization", ["organizationId"]),

  // The keys of the machines (ANH-165). A machine may hold several valid keys
  // at once (the overlap of a rotation); each one is revoked on its own and
  // may expire. Only a salted verifier of a key is kept, never the key, which
  // is shown once, to whoever created it (`convex/machineKeys.ts`).
  machine_api_keys: defineTable({
    machineId: v.id("machines"),
    // The public part of the key `anh1.<selector>.<secret>`: what a request is
    // looked up by. It never opens anything on its own.
    selector: v.string(),
    // `hmac-sha256:1:<salt>:<digest>` of the whole key (convex/lib/crypto.ts).
    keyHash: v.string(),
    // What the dashboard shows of the key: "anh1." and the first 8
    // characters of its selector.
    prefix: v.string(),
    createdAt: v.number(),
    // Absent on a key moved from `machines.apiKey` or issued from the command
    // line (`createKeyByOperator`).
    createdBy: v.optional(v.id("users")),
    // The key opens nothing from this instant on. Set at creation, or by the
    // rotation that replaces the key (end of the overlap).
    expiresAt: v.optional(v.number()),
    // The key opens nothing any more: revoked by someone (`revokedBy`), by a
    // rotation without overlap, or by the job once its expiry passed.
    revokedAt: v.optional(v.number()),
    revokedBy: v.optional(v.id("users")),
    // The key created by the rotation that retired this one.
    replacedBy: v.optional(v.id("machine_api_keys")),
    // The last request it opened, written at most once a minute.
    lastUsedAt: v.optional(v.number()),
  })
    .index("by_selector", ["selector"])
    .index("by_machine", ["machineId"])
    .index("by_machine_and_revoked", ["machineId", "revokedAt"])
    .index("by_revoked_and_expiry", ["revokedAt", "expiresAt"]),

  // Sessions - training sessions (auto, manual), plus the read-only history
  // of the retired ECG recording mode (rows without `kind`).
  sessions: defineTable({
    record: v.optional(recordState),
    recordExpectedFiles: v.optional(v.array(v.string())),
    recordExpectedCount: v.optional(v.number()),
    recordDeleted: v.optional(v.boolean()),
    recordVerifiedCount: v.optional(v.number()),
    recordRejectedCount: v.optional(v.number()),
    organizationId, // Always the machine's organisation
    machineId: v.id("machines"),
    // The rider. Optional only for sessions started at the machine with no
    // rider chosen from the synced roster; every remote launch sets it.
    userId: v.optional(v.id("users")),
    startedById: v.optional(v.id("users")), // Who started the session
    status: v.union(
      v.literal("pending"),
      v.literal("active"),
      // An active session of which the server had no news for
      // SESSION_SILENCE_MS (lib/training.ts): its end is unknown. It goes back
      // to "active" when a heartbeat of its machine names it again, and ends
      // as any active session does when the machine sends its end.
      v.literal("interrupted"),
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
    // The start as the machine dated it, on its own clock (unix ms), for a
    // session whose machine said how long ago it started it. Such a machine
    // dates its points and its events as that start plus the time elapsed:
    // with `startedAt`, which the server then dates, it is what places them
    // on the server's clock (`convex/training.ts`, `machineAxis`). Absent on
    // every other session, whose dates are stored and served as written.
    machineStartedAt: v.optional(v.number()),
    stopRequestedAt: v.optional(v.number()),
    endReason: v.optional(v.string()),
    // The stable code of a launch from the dashboard that failed before its
    // machine armed it (ANH-144): expired, launch right withdrawn, rider's
    // physiology changed... One of the arming refusals of
    // `convex/training.ts`, also a code of contracts/machine-api.json. The
    // site words it from this code; `endReason` says it in English. Absent
    // on every other session.
    endCode: v.optional(v.string()),
    // When the server marked the session "interrupted" (server's clock).
    // Present exactly while the status is "interrupted".
    interruptedAt: v.optional(v.number()),
  })
    .index("by_user", ["userId"])
    // The open sessions of one rider (pending, active, interrupted): a rider
    // has one session at a time, on any machine (`launchAutoSession`).
    .index("by_user_and_status", ["userId", "status"])
    .index("by_machine_and_local_ref", ["machineId", "localRef"])
    .index("by_machine", ["machineId"])
    .index("by_machine_and_status", ["machineId", "status"])
    .index("by_started_by", ["startedById"])
    .index("by_organization", ["organizationId"])
    // The active or pending sessions of one organisation, or of all of them:
    // what the dashboard counts (`dashboard.counters`), and the active ones
    // `training.interruptSilentSessions` reads, a bounded page at a time.
    .index("by_status_and_organization", ["status", "organizationId"])
    // The lists of sessions (`convex/sessions.ts`), most recent start first.
    .index("by_started", ["startedAt"])
    .index("by_status_and_started", ["status", "startedAt"])
    .index("by_organization_and_started", ["organizationId", "startedAt"])
    .index("by_organization_and_status_and_started", [
      "organizationId",
      "status",
      "startedAt",
    ])
    .index("by_user_and_started", ["userId", "startedAt"])
    .index("by_machine_and_started", ["machineId", "startedAt"])
    // The bounded lists of a gestionnaire (ANH-227): one organisation and
    // one machine or rider, with the requested status in the index range.
    .index("by_organization_and_machine_and_started", [
      "organizationId",
      "machineId",
      "startedAt",
    ])
    .index("by_organization_and_machine_and_status_and_started", [
      "organizationId",
      "machineId",
      "status",
      "startedAt",
    ])
    .index("by_organization_and_user_and_started", [
      "organizationId",
      "userId",
      "startedAt",
    ])
    .index("by_organization_and_user_and_status_and_started", [
      "organizationId",
      "userId",
      "status",
      "startedAt",
    ]),

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
  // nothing computes or stores a summary here any more. The summaries of the
  // training sessions are in `session_summaries_v2`.
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

  // The summary of a finished training session (ANH-89), one row per
  // session, written only by `sessionReports` (internal functions). The
  // provisional one comes from the 1 Hz telemetry when the session ends
  // (`source: "telemetry"`); the one of the deposited record replaces it for
  // good (`source: "record"`). Health data: it is read only through
  // `sessionReports.getSummary` and `sessionReports.riderHistory`, under the
  // rights of the session it belongs to (`canAccessSession`).
  session_summaries_v2: defineTable({
    organizationId, // Always the session's organisation
    sessionId: v.id("sessions"),
    source: summarySourceValidator,
    computedAt: v.number(),
    // Why the deposited record could not be summarised, as a code; the
    // provisional summary then stays. Absent on every other row.
    recordProblem: v.optional(v.string()),
    figures: summaryFiguresValidator,
  })
    .index("by_session", ["sessionId"])
    .index("by_organization", ["organizationId"]),

  // The rider's feedback after a training session with someone on board
  // (ANH-146), one row per session at most, written only by
  // `convex/sessionFeedback.ts`: by the rider from the site, or by the
  // operator from the machine's console, within `EDIT_WINDOW_MS` of the end;
  // the last write wins. No row: no feedback. Health data: read only under
  // the rights of its session, and exported without the free text. The
  // scale is `contracts/feedback.json` (`convex/lib/feedback.ts`).
  session_feedback: defineTable({
    organizationId, // Always the session's organisation
    sessionId: v.id("sessions"),
    nauseaScore: v.number(), // whole number of the scale
    dizzinessScore: v.number(), // whole number of the scale
    comfort: feedbackComfortValidator,
    freeText: v.optional(v.string()), // trimmed, never empty
    submittedBy: feedbackSubmitterValidator,
    submittedAt: v.number(), // server's clock
  })
    // One row per session: every write reads it first (`upsert`).
    .index("by_session", ["sessionId"])
    // The export (`exportFeedback`), newest first: one organisation, or all.
    .index("by_organization_and_submittedAt", ["organizationId", "submittedAt"])
    .index("by_submittedAt", ["submittedAt"]),

  // Training telemetry, ~1 Hz, as the Pi reads it back from the session's
  // local record. One row per (sessionId, t): a point sent twice is stored
  // once (`storeTelemetry`).
  training_telemetry: defineTable({
    organizationId, // Always the session's organisation
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    t: v.number(), // unix ms, the machine's clock (see `machineAxis` in training.ts)
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

  machine_health_history: defineTable({
    machineId: v.id("machines"),
    organizationId,
    bucket: v.number(),
    updatedAt: v.number(),
    samples: v.number(),
    min: healthMeasurementsValidator,
    max: healthMeasurementsValidator,
    last: machineHealthValidator,
  })
    .index("by_machine_and_bucket", ["machineId", "bucket"])
    .index("by_bucket", ["bucket"]),

  // Machine Heartbeats - For monitoring (can be cleaned up periodically)
  session_record_files: defineTable({
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    organizationId: v.id("organizations"),
    path: v.string(),
    storageId: v.id("_storage"),
    sha256: v.string(),
    size: v.number(),
    uploadedAt: v.number(),
    verified: v.boolean(),
    rejection: v.optional(v.string()),
    lastVerificationAt: v.optional(v.number()),
  })
    .index("by_session", ["sessionId"])
    .index("by_session_and_path", ["sessionId", "path"])
    .index("by_machine", ["machineId"]),

  session_record_uploads: defineTable({
    sessionId: v.id("sessions"),
    machineId: v.id("machines"),
    organizationId: v.id("organizations"),
    path: v.string(),
    sha256: v.string(),
    size: v.number(),
    nonce: v.string(),
    createdAt: v.number(),
    revoked: v.boolean(),
  })
    .index("by_session_and_path", ["sessionId", "path"])
    .index("by_session", ["sessionId"])
    .index("by_nonce", ["nonce"]),

  session_record_manifest_pages: defineTable({
    sessionId: v.id("sessions"),
    storageId: v.id("_storage"),
    bucket: v.number(),
    page: v.number(),
    paths: v.array(v.string()),
  })
    .index("by_session", ["sessionId"])
    .index("by_session_and_bucket", ["sessionId", "bucket"])
    .index("by_session_bucket_page", ["sessionId", "bucket", "page"]),

  record_upload_sweeps: defineTable({
    name: v.literal("uploads"),
    cutoff: v.number(),
  }).index("by_name", ["name"]),

  machine_heartbeats: defineTable({
    machineId: v.id("machines"),
    timestamp: v.number(),
    batteryLevel: v.optional(v.number()),
    wifiStrength: v.optional(v.number()),
    activeSessionId: v.optional(v.id("sessions")),
  })
    .index("by_machine_and_timestamp", ["machineId", "timestamp"])
    .index("by_timestamp", ["timestamp"]),

  // Alerts to the gestionnaires and organisation admins (ANH-90): one row per
  // recipient, in the organisation of the machine it is about. Written by the
  // server where it detects the event (convex/lib/notifications.ts), read by
  // its recipient only (convex/notifications.ts). No health data and no
  // passenger name: a type, a machine, a session, a machine-side code.
  notifications: defineTable({
    organizationId: v.id("organizations"), // The machine's organisation
    userId: v.id("users"), // The recipient
    type: notificationTypeValidator,
    machineId: v.id("machines"),
    sessionId: v.optional(v.id("sessions")),
    // A code the machine sent, never a sentence: the drive's fault mnemonic
    // ("OHF"), or the action a safety verdict demanded ("RAMP_DOWN").
    detail: v.optional(v.string()),
    // How many events this row stands for: a new event of the same kind
    // folds into the recipient's unread row instead of adding one.
    count: v.number(),
    createdAt: v.number(),
    updatedAt: v.number(),
    read: v.boolean(),
    // Legacy component identifier, retained for existing deployments, never written.
    emailId: v.optional(v.string()),
    // Delivery state expires and is deleted with the recipient's alert.
    email: v.optional(
      v.object({
        type: v.optional(notificationTypeValidator),
        status: v.union(
          v.literal("pending"),
          v.literal("sent"),
          v.literal("failed"),
        ),
        attempts: v.number(),
        queuedAt: v.number(),
        updatedAt: v.number(),
        resendId: v.optional(v.string()),
        failureCode: v.optional(v.string()),
      }),
    ),
  })
    .index("by_user_and_organization_and_updated", [
      "userId",
      "organizationId",
      "updatedAt",
    ])
    .index("by_user_and_organization_and_read", [
      "userId",
      "organizationId",
      "read",
    ])
    .index("by_user_and_organization_and_machine_and_updated", [
      "userId",
      "organizationId",
      "machineId",
      "updatedAt",
    ])
    .index("by_user_and_organization_and_machine_and_read", [
      "userId",
      "organizationId",
      "machineId",
      "read",
    ])
    .index("by_user", ["userId"])
    .index("by_email_status_and_updated", ["email.status", "email.updatedAt"])
    .index("by_updated", ["updatedAt"]),

  // Each account's choice of alerts sent by e-mail too (ANH-90). A type left
  // unset takes its default (`NOTIFICATION_TYPES`, convex/lib/notifications.ts).
  notification_settings: defineTable({
    userId: v.id("users"),
    email: v.object(notificationEmailFields),
    updatedAt: v.number(),
  }).index("by_user", ["userId"]),
});
