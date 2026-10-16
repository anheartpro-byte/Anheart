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
] as const;
export type AuditTargetType = (typeof AUDIT_TARGET_TYPES)[number];
export const auditTargetTypeValidator = v.union(
  ...AUDIT_TARGET_TYPES.map((target) => v.literal(target)),
);

export const AUDIT_ACTIONS = [
  "user.role_changed", // Admin changes a user's role; target: user.
  "user.invited", // Staff prepares an invitation; target: invited user.
  "user.linked_to_account", // Staff links a patient to an account; target: user.
  "user.deleted", // Admin deletes a user; target: deleted user.
  "patient_gestionnaire.assigned", // Staff assigns a manager; target: patient user.
  "patient_gestionnaire.removed", // Staff removes a manager; target: patient user.
  "gestionnaire_patients.set", // Staff sets a manager's patients; target: manager user.
  "machine_gestionnaire.assigned", // Staff assigns a manager; target: machine.
  "machine_gestionnaire.removed", // Staff removes a manager; target: machine.
  "machine_gestionnaires.set", // Staff sets a machine's managers; target: machine.
  "gestionnaire_machines.set", // Staff sets a manager's machines; target: manager user.
  "launch_right.granted", // Staff grants launch access; target: passenger user.
  "launch_right.revoked", // Staff revokes launch access; target: passenger user.
  "membership.created", // Clerk webhook creates membership; target: user.
  "membership.role_changed", // Clerk webhook changes membership role; target: user.
  "membership.deactivated", // Clerk webhook deactivates membership; target: user.
  "account.deleted_in_clerk", // Clerk webhook deletes an account; target: user.
  "organization.deleted", // Clerk webhook deletes an organisation; target: organisation.
  "physiology.updated", // Staff updates physiology, without values; target: user.
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
  "machine.deleted", // Admin deletes a machine; target: machine.
  "machine.restored", // Admin restores a machine; target: machine.
  "registry.identity_updated", // Admin updates registry identity; target: machine.
  "registry.radii_recorded", // Admin records radii, without values; target: machine.
  "registry.drive_sheet_attached", // Admin attaches a drive sheet; target: machine.
  "registry.review_added", // Admin adds a validation review; target: machine.
  "registry.level_lowered", // Admin lowers validation level; target: machine.
  "software_release.recorded", // Admin records a release; target: software release.
  "medical_parameters.registered", // Operator registers a version; target: medical version.
  "record.deleted", // Authorised user deletes a recording; target: session.
] as const;
export type AuditAction = (typeof AUDIT_ACTIONS)[number];
export const auditActionValidator = v.union(
  ...AUDIT_ACTIONS.map((action) => v.literal(action)),
);

export const AUDIT_ACTION_TARGET: Record<AuditAction, AuditTargetType> = {
  "user.role_changed": "user",
  "user.invited": "user",
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
    "account.deleted_in_clerk",
    "organization.deleted",
  ],
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
  "fitness.submitFitnessAnswers":
    "Answers are health data; the audited decision is fitness.validated.",
  "machineHealth.purge": "Automatic retention of machine health samples.",
  "machineKeys.recordKeyUse":
    "Operational key-use timestamp, without a rights change.",
  "machineRegistry.generateUploadUrl":
    "Upload preparation; attachment is audited separately.",
  "machines.updateMachine": "Name and location only.",
  "machines.recordHeartbeat":
    "Operational machine state, without a human decision.",
  "machines.checkOfflineMachines":
    "Automatic offline detection and session interruption.",
  "notifications.markRead": "Recipient marks an alert read.",
  "notifications.markAllRead": "Recipient marks alerts read.",
  "notifications.updateSettings": "Recipient changes notification preferences.",
  "notifications.deliver": "Delivery of an operational alert.",
  "notifications.purge": "Automatic retention of notifications.",
  "patientInvitations.finish":
    "Delivery status of an already audited invitation.",
  "recordStorage.requestUpload": "Machine prepares a record upload.",
  "recordStorage.commit": "Machine completes a record upload.",
  "recordStorage.finishVerification":
    "Automatic verification of uploaded data.",
  "recordStorage.manifestPage": "Machine provides upload manifest identifiers.",
  "recordStorage.sweepUploads": "Automatic cleanup of abandoned uploads.",
  "recordStorage.removeBatch": "Executes an already audited deletion.",
  "recordStorage.enforceRetention":
    "Automatic retention policy, no human decision; kept on the session row.",
  "sessionReports.storeSummary":
    "Machine stores a session summary containing health data.",
  "sessionReports.noteRecordProblem":
    "Machine reports an operational recording problem.",
  "training.sweepPendingSessions": "Automatic expiry of a launch never armed.",
  "training.updateLive": "Transient live machine state containing health data.",
  "training.syncProfiles":
    "Machine synchronises training profiles containing health data.",
  "training.storeTelemetry": "Machine stores health telemetry.",
  "training.storeEvents":
    "Machine stores session events, without a human decision.",
  "users.getOrCreateUser":
    "Mirror of Clerk session claims; the decision arrives through the Clerk webhook.",
  "users.updateUserProfile": "Own name and language only.",
  "users.updatePatient": "Patient name and language only.",
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
    details: entry.details,
    requestId,
    source: entry.source,
  });
}
