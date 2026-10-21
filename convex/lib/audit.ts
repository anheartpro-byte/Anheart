import { v, type Infer } from "convex/values";
import type { Id } from "../_generated/dataModel";
import type { MutationCtx } from "../_generated/server";
import type { CurrentUser, Role } from "./auth";
import {
  MACHINE_VALIDATION_LEVELS,
  VALIDATION_LEVELS,
} from "./releaseValidation";

export const AUDIT_SOURCES = [
  "site",
  "machine",
  "cron",
  "webhook",
  // EX-1 extension: deployment-key CLI calls, always acting as the system.
  "operator",
] as const;
export type AuditSource = (typeof AUDIT_SOURCES)[number];
export const auditSourceValidator = v.union(
  ...AUDIT_SOURCES.map((source) => v.literal(source)),
);

export const AUDIT_ACTOR_ROLES = [
  "admin",
  "org_admin",
  "gestionnaire",
  "user",
  "machine",
  "system",
] as const;
export type AuditActorRole = (typeof AUDIT_ACTOR_ROLES)[number];
export const auditActorRoleValidator = v.union(
  ...AUDIT_ACTOR_ROLES.map((role) => v.literal(role)),
);

export const AUDIT_TARGET_TYPES = [
  "user",
  "machine",
  "machine_key",
  "session",
  "fitness_assessment",
  "consent",
  "organization",
  "software_release",
  "medical_parameters_version",
  "patient_invitation",
] as const;
export type AuditTargetType = (typeof AUDIT_TARGET_TYPES)[number];
export const auditTargetTypeValidator = v.union(
  ...AUDIT_TARGET_TYPES.map((target) => v.literal(target)),
);

export const AUDIT_ACTIONS = [
  "user.role_changed", // Admin changes a user's role; target: user.
  "user.invited", // Staff prepares an invitation; target: invitation, even without a unique profile.
  "user.linked_to_account", // Patient or Clerk webhook links a verified account; target: user.
  "user.deleted", // Authorised staff removes a user from their scope; target: user.
  "patient_gestionnaire.assigned", // Staff or Clerk webhook assigns a manager; target: patient user.
  "patient_gestionnaire.removed", // Staff removes a manager; target: patient user.
  "gestionnaire_patients.set", // Staff sets a manager's patients; target: manager user.
  "machine_gestionnaire.assigned", // Staff assigns a manager; target: machine.
  "machine_gestionnaire.removed", // Staff removes a manager; target: machine.
  "machine_gestionnaires.set", // Staff sets a machine's managers; target: machine.
  "gestionnaire_machines.set", // Staff sets a manager's machines; target: manager user.
  "launch_right.granted", // Staff grants launch access; target: passenger user.
  "launch_right.revoked", // Staff revokes launch access; target: passenger user.
  "membership.created", // Login mirror or Clerk webhook activates membership; target: user.
  "membership.role_changed", // Login mirror or Clerk webhook changes an active membership role; target: user.
  "membership.deactivated", // Clerk webhook deactivates membership; target: user.
  "account.deleted_in_clerk", // Clerk webhook deletes an account; target: user.
  "organization.deleted", // Clerk webhook deletes an organisation; target: organisation.
  "physiology.updated", // Staff updates physiology, without values; target: user.
  "fitness.submitted", // Rider or authorised staff submits answers, without answers or verdict; target: assessment.
  "fitness.validated", // Staff validates fitness, without verdict; target: assessment.
  "consent.signed", // User signs consent; target: consent.
  "consent.withdrawn", // User withdraws consent; target: consent.
  "session.launched", // Authorised user requests launch; target: session.
  "session.cancelled", // Authorised user cancels a pending launch; target: session.
  "session.stop_requested", // Authorised user requests a stop; target: session.
  "session.started", // Machine confirms start; target: session.
  "session.local_registered", // Machine registers a local session; target: session.
  "session.ended", // Machine records the end, without reason or measures; target: session.
  "machine_key.created", // Admin or operator creates a key; target: machine key.
  "machine_key.revoked", // Admin or operator revokes a key; target: machine key.
  "machine_key.expired_revoked", // Cron revokes an expired key; target: machine key.
  "machine.created", // Admin creates a machine; target: machine.
  "machine.deleted", // Authorised machine manager soft-deletes a machine; target: machine.
  "machine.restored", // Admin restores a machine; target: machine.
  "registry.identity_updated", // Admin updates registry identity; target: machine.
  "registry.radii_recorded", // Admin records radii, without values; target: machine.
  "registry.drive_sheet_attached", // Admin attaches a drive sheet; target: machine.
  "registry.review_added", // Admin adds a validation review; target: machine.
  "registry.level_lowered", // Admin lowers validation level; target: machine.
  "software_release.recorded", // Admin records a release; target: software release.
  "medical_parameters.registered", // Operator registers a version; target: medical version.
  "record.deleted", // Authorised user or retention cron deletes a recording; target: session.
] as const;
export type AuditAction = (typeof AUDIT_ACTIONS)[number];
export const auditActionValidator = v.union(
  ...AUDIT_ACTIONS.map((action) => v.literal(action)),
);

export const AUDIT_ACTION_TARGET: Record<AuditAction, AuditTargetType> = {
  "user.role_changed": "user",
  "user.invited": "patient_invitation",
  "user.linked_to_account": "user",
  "user.deleted": "user",
  "patient_gestionnaire.assigned": "user",
  "patient_gestionnaire.removed": "user",
  "gestionnaire_patients.set": "user",
  "machine_gestionnaire.assigned": "machine",
  "machine_gestionnaire.removed": "machine",
  "machine_gestionnaires.set": "machine",
  "gestionnaire_machines.set": "user",
  "launch_right.granted": "user",
  "launch_right.revoked": "user",
  "membership.created": "user",
  "membership.role_changed": "user",
  "membership.deactivated": "user",
  "account.deleted_in_clerk": "user",
  "organization.deleted": "organization",
  "physiology.updated": "user",
  "fitness.submitted": "fitness_assessment",
  "fitness.validated": "fitness_assessment",
  "consent.signed": "consent",
  "consent.withdrawn": "consent",
  "session.launched": "session",
  "session.cancelled": "session",
  "session.stop_requested": "session",
  "session.started": "session",
  "session.local_registered": "session",
  "session.ended": "session",
  "machine_key.created": "machine_key",
  "machine_key.revoked": "machine_key",
  "machine_key.expired_revoked": "machine_key",
  "machine.created": "machine",
  "machine.deleted": "machine",
  "machine.restored": "machine",
  "registry.identity_updated": "machine",
  "registry.radii_recorded": "machine",
  "registry.drive_sheet_attached": "machine",
  "registry.review_added": "machine",
  "registry.level_lowered": "machine",
  "software_release.recorded": "software_release",
  "medical_parameters.registered": "medical_parameters_version",
  "record.deleted": "session",
};

const detailRoleValidator = v.union(
  v.literal("admin"),
  v.literal("org_admin"),
  v.literal("gestionnaire"),
  v.literal("user"),
);
const detailLevelValidator = v.union(
  ...[...new Set([...VALIDATION_LEVELS, ...MACHINE_VALIDATION_LEVELS])].map(
    (level) => v.literal(level),
  ),
);

// No health data or user-entered free text. Adding a key requires review.
// String values are field names, identifiers or versions, never their content.
export const auditDetailsValidator = v.object({
  fields: v.optional(v.array(v.string())),
  role: v.optional(detailRoleValidator),
  previousRole: v.optional(detailRoleValidator),
  level: v.optional(detailLevelValidator),
  previousLevel: v.optional(detailLevelValidator),
  version: v.optional(v.string()),
  softwareVersion: v.optional(v.string()),
  medicalParametersVersion: v.optional(v.string()),
  machineId: v.optional(v.id("machines")),
  userId: v.optional(v.id("users")),
  gestionnaireId: v.optional(v.id("users")),
  added: v.optional(v.array(v.string())),
  removed: v.optional(v.array(v.string())),
  count: v.optional(v.number()),
  outcome: v.optional(v.union(v.literal("completed"), v.literal("failed"))),
  expiresAt: v.optional(v.number()),
});
export const AUDIT_DETAIL_KEYS = [
  "fields",
  "role",
  "previousRole",
  "level",
  "previousLevel",
  "version",
  "softwareVersion",
  "medicalParametersVersion",
  "machineId",
  "userId",
  "gestionnaireId",
  "added",
  "removed",
  "count",
  "outcome",
  "expiresAt",
] as const;

// Actions (including HTTP actions) do not write the database. Their writes pass
// through these mutations, e.g. users.createPatient -> patientInvitations.prepare
// and medicalParameters.registerVersion -> medicalParametersStore.insertVersion.
export const SENSITIVE_MUTATIONS: Readonly<
  Record<string, readonly AuditAction[]>
> = {
  "clerkWebhook.applyEvent": [
    "membership.created",
    "membership.role_changed",
    "membership.deactivated",
    "user.linked_to_account",
    "patient_gestionnaire.assigned",
    "account.deleted_in_clerk",
    "organization.deleted",
  ],
  "users.getOrCreateUser": ["membership.created", "membership.role_changed"],
  "fitness.submitFitnessAnswers": ["fitness.submitted"],
  "fitness.validateFitnessAssessment": ["fitness.validated"],
  "fitness.signConsent": ["consent.signed"],
  "fitness.withdrawConsent": ["consent.withdrawn"],
  "machineKeys.createMachineKey": [
    "machine_key.created",
    "machine_key.revoked",
  ],
  "machineKeys.revokeMachineKey": ["machine_key.revoked"],
  "machineKeys.revokeExpiredKeys": ["machine_key.expired_revoked"],
  "machineKeys.createKeyByOperator": [
    "machine_key.created",
    "machine_key.revoked",
  ],
  "machineKeys.revokeKeyByOperator": ["machine_key.revoked"],
  "machineRegistry.updateIdentity": ["registry.identity_updated"],
  "machineRegistry.recordRadii": ["registry.radii_recorded"],
  "machineRegistry.attachDriveParameterSheet": [
    "registry.drive_sheet_attached",
  ],
  "machineRegistry.addValidationReview": ["registry.review_added"],
  "machineRegistry.lowerValidationLevel": ["registry.level_lowered"],
  "machines.createMachine": [
    "machine.created",
    "machine_key.created",
    "machine_gestionnaires.set",
  ],
  "machines.assignMachineToGestionnaires": ["machine_gestionnaires.set"],
  "machines.setGestionnaireMachines": ["gestionnaire_machines.set"],
  "machines.deleteMachine": ["machine.deleted"],
  "machines.restoreMachine": ["machine.restored"],
  "machines.assignGestionnaireToMachine": ["machine_gestionnaire.assigned"],
  "machines.removeGestionnaireFromMachine": ["machine_gestionnaire.removed"],
  "medicalParametersStore.insertVersion": ["medical_parameters.registered"],
  "patientInvitations.prepare": ["user.invited"],
  "recordStorage.remove": ["record.deleted"],
  "recordStorage.enforceRetention": ["record.deleted"],
  "softwareReleases.recordRelease": ["software_release.recorded"],
  "training.grantLaunchRight": ["launch_right.granted"],
  "training.revokeLaunchRight": ["launch_right.revoked"],
  "training.setUserPhysiology": ["physiology.updated"],
  "training.launchAutoSession": ["session.launched"],
  "training.requestStop": ["session.cancelled", "session.stop_requested"],
  "training.markTrainingStarted": ["session.started"],
  "training.registerLocalSession": ["session.local_registered"],
  "training.endTrainingSession": ["session.ended"],
  "users.updateUserRole": ["user.role_changed"],
  "users.deleteUser": ["user.deleted"],
  "users.linkPatientToClerk": ["user.linked_to_account"],
  "users.assignGestionnaireToUser": ["patient_gestionnaire.assigned"],
  "users.removeGestionnaireFromUser": ["patient_gestionnaire.removed"],
  "users.assignPatientsToGestionnaire": ["gestionnaire_patients.set"],
};

export const NON_SENSITIVE_MUTATIONS: Readonly<Record<string, string>> = {
  "machineHealth.purge":
    "Cron removes expired heartbeat and machine health history samples.",
  "machineKeys.recordKeyUse":
    "Machine HTTP authentication refreshes the key-use timestamp, without a rights change.",
  "machineRegistry.generateUploadUrl":
    "Anheart admin prepares an upload URL; registry attachment is audited separately.",
  "machines.updateMachine":
    "Authorised machine manager changes only name and location.",
  "machines.recordHeartbeat":
    "Machine HTTP heartbeat records operational state and health, without a rights change.",
  "machines.checkOfflineMachines":
    "Cron detects offline machines and interrupts silent sessions, without confirming their end.",
  "notifications.continueMarkAllRead":
    "Scheduled continuation marks the recipient visible alerts read in bounded batches.",
  "notifications.markRead": "Recipient marks an alert read.",
  "notifications.markAllRead": "Recipient marks alerts read.",
  "notifications.updateSettings": "Recipient changes notification preferences.",
  "notifications.deliver":
    "Scheduled alert delivery writes recipients' operational notifications.",
  "notifications.deliverRecipient":
    "Writes one recipient's operational alert in an isolated subtransaction.",
  "notifications.purge": "Cron removes expired notifications.",
  "notificationEmails.beginAttempt":
    "Claims an operational alert e-mail attempt without changing access rights.",
  "notificationEmails.finishAttempt":
    "Records operational e-mail delivery and bounded retries without changing access rights.",
  "notificationEmails.abandonPending":
    "Daily cleanup records abandoned operational e-mails without changing access rights.",
  "patientInvitations.finish":
    "Patient creation action records delivery status of an already audited invitation.",
  "recordStorage.requestUpload": "Machine prepares a record upload.",
  "recordStorage.commit": "Machine completes a record upload.",
  "recordStorage.finishVerification":
    "Internal upload verification action records verification results and recording status.",
  "recordStorage.manifestPage":
    "Internal upload verification action stores manifest path pages.",
  "recordStorage.sweepUploads":
    "Cron recovers uploaded files whose commit was missed and schedules verification.",
  "recordStorage.removeBatch":
    "Scheduled batches remove files after an audited deletion or configured automatic retention.",
  "sessionReports.storeSummary":
    "Internal summary actions store the computed session summary containing health data.",
  "sessionReports.noteRecordProblem":
    "Internal record summary action annotates the provisional summary with a recording problem.",
  "training.sweepPendingSessions":
    "Cron fails pending launches that expired or no longer pass arming checks.",
  "training.updateLive":
    "Machine HTTP heartbeat updates transient live training state containing health data.",
  "training.syncProfiles":
    "Machine synchronises its saved training programme profiles, without a rights change.",
  "training.storeTelemetry": "Machine stores health telemetry.",
  "training.storeEvents":
    "Machine stores session events, without a human decision.",
  "users.updateUserProfile": "Own name and language only.",
  "users.updatePatient":
    "Authorised staff changes only a patient's name and language.",
  "migrations/retireLegacyRecording.failOpenRecordingSessions":
    "One-off operator data migration retires open legacy recording sessions, without a product decision.",
  "migrations/retireLegacyRecording.removeMachineConfig":
    "One-off operator data migration removes retired recording configuration, without a product decision.",
  "migrations/machineApiKeys.moveMachineKeysToTable":
    "One-off operator data migration moves legacy machine keys into their table, without a product decision.",
  "migrations/multiOrganization.attachExistingRowsToAnheart":
    "One-off operator data migration attaches existing rows to Anheart, without a product decision.",
  "migrations/multiOrganization.repairDuplicatedAnheartOrganization":
    "One-off operator data migration merges duplicated Anheart organizations, without a product decision.",
};

export type AuditActor =
  | { kind: "user"; id: Id<"users">; role: Role }
  | { kind: "machine"; id: Id<"machines"> }
  | { kind: "system" };

export function userActor(
  caller: CurrentUser,
): Extract<AuditActor, { kind: "user" }> {
  return { kind: "user", id: caller._id, role: caller.role };
}

export function machineActor(
  machineId: Id<"machines">,
): Extract<AuditActor, { kind: "machine" }> {
  return { kind: "machine", id: machineId };
}

export const SYSTEM_ACTOR = { kind: "system" } as const satisfies AuditActor;

type AuditEntry = {
  organizationId: Id<"organizations"> | undefined;
  action: AuditAction;
  targetId: string;
  details?: Infer<typeof auditDetailsValidator>;
} & (
  | { source: Exclude<AuditSource, "operator">; actor: AuditActor }
  | { source: "operator"; actor: Extract<AuditActor, { kind: "system" }> }
);

function boundedDetails(details: AuditEntry["details"]): AuditEntry["details"] {
  if (
    !details ||
    ((details.added?.length ?? 0) <= 1000 &&
      (details.removed?.length ?? 0) <= 1000)
  )
    return details;
  // Convex arrays are limited to 8192 entries. Keep at most 1000 identifiers
  // per list; count is the total before truncation across both lists.
  return {
    ...details,
    added: details.added?.slice(0, 1000),
    removed: details.removed?.slice(0, 1000),
    count: (details.added?.length ?? 0) + (details.removed?.length ?? 0),
  };
}

/**
 * One request-metadata read and one insertion, no other read or write. A typed
 * entry adds no refusal or throw path, so auditing cannot refuse a safety stop.
 * Database/runtime failures remain transaction failures, as for any insertion.
 * The type permits operator calls only with a system actor. Target types come
 * from the action map, never from the caller. Request IDs are shared with the
 * originating mutation, action or HTTP request, including nested mutations.
 */
export async function audit(
  ctx: MutationCtx,
  entry: AuditEntry,
): Promise<void> {
  const { requestId } = await ctx.meta.getRequestMetadata();
  const actorRole =
    entry.actor.kind === "user" ? entry.actor.role : entry.actor.kind;
  const actorId = entry.actor.kind === "system" ? undefined : entry.actor.id;
  await ctx.db.insert("audit_log", {
    at: Date.now(),
    organizationId: entry.organizationId,
    actorId,
    actorRole,
    action: entry.action,
    targetType: AUDIT_ACTION_TARGET[entry.action],
    targetId: entry.targetId,
    details: boundedDetails(entry.details),
    requestId,
    source: entry.source,
  });
}
