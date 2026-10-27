/**
 * The stable error codes of the functions the dashboard calls, in one place.
 *
 * Convex masks the text of a plain `Error` in production: the site would only
 * show a generic sentence with a request reference. A `ConvexError` reaches
 * the browser as it is thrown. A refusal is therefore a `ConvexError` whose
 * data is one of the codes below, never a sentence: the site translates the
 * code (`errors.<code>` in `messages/fr.json` and `messages/en.json`, read by
 * `lib/feedback.ts`). `convex/errors.test.ts` checks that every code has both
 * translations, that no translation is left without its code, and that no
 * backend source throws a code missing from this table.
 *
 * The codes of the machine API (the Raspberry Pi's HTTP routes) are another
 * list, shared with the Pi: `contracts/machine-api.json`, read by
 * `convex/lib/contract.ts`.
 *
 * Each entry is the code and what it means, in English, for whoever reads the
 * server's logs or this file. What the user reads is the translation.
 */
import { ConvexError } from "convex/values";

export const DASHBOARD_ERRORS = {
  // The caller's sign-in (`requireAuth`, `getCurrentUserOrThrow`, ANH-90).
  not_authenticated:
    "The call carries no valid Clerk token: the visitor is signed out, or the session expired.",
  account_not_registered:
    "The Clerk account has no profile on the server: not created yet by the webhook, or deleted.",

  // The organisation of the call (`organizationFromClaims`, `sessionOf`),
  // also the values `users.getCurrentUser` returns as `organizationAccess`.
  no_active_organization:
    "The token names no active organisation while ANHEART_ORG_ID is set.",
  organization_not_synchronized:
    "The token's organisation is not, or no longer, known to the server.",
  unknown_organization_role:
    "The token's organisation role is none of admin, gestionnaire and patient.",
  malformed_organization_claim: "The token's org_id is not a string.",
  no_organization:
    "The account has no organisation, or no active membership in its main one.",

  // The caller's account and organisation.
  not_authorized: "The caller's role does not allow this call.",
  profile_pending:
    "The account's profile is waiting for Clerk: the call can be retried shortly.",
  profile_reconciliation_required:
    "Two profiles claim the same person: an administrator must review them.",
  roles_managed_in_clerk:
    "Roles are changed in Clerk Organizations once ANHEART_ORG_ID is set.",
  shared_profile_admin_only:
    "The profile belongs to several organisations: only an Anheart administrator may change it.",
  organization_not_found:
    "The organisation does not exist, or was deleted in Clerk.",
  rate_limited:
    "The account is over its write limit: retry later. Thrown by `limitUser` (convex/lib/rateLimits.ts) as `{code, message, retryAfterMs}`.",

  // Accounts, patients and gestionnaires.
  user_not_found: "The account does not exist (any more).",
  user_not_managed: "The caller does not manage this account.",
  patient_not_managed: "The caller does not manage this patient.",
  fitness_answers_invalid:
    "The answers do not match the current questionnaire.",
  questionnaire_outdated: "The questionnaire version is not current.",
  fitness_assessment_not_found:
    "The fitness assessment does not exist or is outside the caller's scope.",
  fitness_self_validation:
    "A passenger cannot validate their own fitness assessment.",
  fitness_assessment_superseded:
    "A newer fitness assessment has been submitted.",
  fitness_already_validated:
    "This fitness assessment has already been validated.",
  invalid_validity: "The fitness validity date is missing or invalid.",
  fitness_notes_too_long: "Fitness review notes exceed 1000 characters.",
  consent_text_outdated: "The consent text version is not current.",
  consent_not_active: "No active consent exists to withdraw.",
  rider_not_fit: "The rider has no current, validated fit assessment.",
  fitness_expired: "The rider's fitness assessment has expired.",
  consent_missing: "The rider has no active consent for the current text.",
  cannot_delete_self: "An account cannot delete itself.",
  only_patients_deletable: "A gestionnaire deletes patient accounts only.",
  not_a_patient:
    "The account is not a patient of the organisation the link would live in.",
  gestionnaire_not_found: "The gestionnaire account does not exist here.",
  not_a_gestionnaire:
    "The account is not a gestionnaire of the organisation concerned.",
  gestionnaire_self_only: "A gestionnaire may only add or remove themselves.",
  gestionnaire_already_assigned: "The gestionnaire already has this link.",
  gestionnaire_not_assigned: "The gestionnaire has no such link.",

  // Machines.
  machine_not_found:
    "The machine does not exist, belongs to another organisation, or was deleted.",
  machine_not_managed: "The caller does not manage this machine.",
  machine_in_session: "The machine is in a session: it cannot be deleted.",
  machine_has_pending_session:
    "A session waits for this machine: it cannot be deleted before it is cancelled.",
  machine_not_deleted:
    "The machine is not deleted: there is nothing to restore.",
  last_gestionnaire:
    "The caller is the machine's only gestionnaire: they cannot leave it.",

  // One session at a time (ANH-144). Thrown by `launchAutoSession`.
  rider_busy:
    "The rider already has a session waiting, running, or interrupted on a machine silent since, on any machine: one at a time. Also an arming refusal (below).",
  machine_busy:
    "The machine already has a session waiting, running, or interrupted while it is silent: one at a time.",

  // Launches refused when their machine arms them (ANH-144): the codes of
  // `ARMING_REFUSALS` (convex/training.ts), `rider_busy` above among them.
  // Never thrown at the dashboard: each is the `endCode` of the failed
  // session, which the site words with its translation, and a code of the
  // machine API (contracts/machine-api.json, `arming_refusals`).
  expired_before_start:
    "End code: the machine did not confirm the start within PENDING_SESSION_TTL_S of the launch. A console that arms before confirming may have started.",
  machine_deleted_before_start:
    "End code: the machine was deleted while the launch waited.",
  organization_changed_before_start:
    "End code: the machine left the session's organisation while the launch waited.",
  right_revoked_before_start:
    "End code: whoever launched could no longer launch it, or the rider is no longer a member, when it was checked again.",
  age_refused_before_start:
    "End code: the rider's birth year is no longer known, or gives less than the minimum age.",
  hr_max_changed_before_start:
    "End code: the rider's max heart rate is no longer the one the launch gave the machine.",

  // Machine keys (ANH-165).
  machine_key_not_found:
    "The machine key does not exist, or belongs to a machine the caller does not administer.",
  invalid_key_overlap:
    "The overlap of a new machine key is not a whole number of minutes between 0 and 10080 (7 days).",
  invalid_key_expiry:
    "The lifetime of a new machine key is not a whole number of minutes between 60 (one hour) and 5256000 (ten years).",

  // Patient invitations.
  invalid_invitation:
    "The name or the e-mail address of the invitation is not valid.",
  invitation_unavailable:
    "The invitation cannot be created for this caller and this address.",
  invitation_failed: "Clerk did not confirm that the invitation was sent.",

  // Launch rights and physiology (convex/training.ts, ANH-90).
  launch_right_patients_only:
    "A launch right is granted to a patient of the machine's organisation: staff launch without one.",
  invalid_hr_max: "A max heart rate is a whole number of bpm, from 100 to 220.",
  invalid_birth_year:
    "A birth year is a whole number that gives an age from 10 to 100.",

  // A launch from the dashboard (`training.launchAutoSession`).
  launch_for_self_only: "A patient launches a session for themselves only.",
  launch_right_missing:
    "The patient holds no launch right on this machine, in its organisation.",
  machine_offline:
    "The machine is offline (no heartbeat for LIVE_FRESH_MS): it cannot take a launch.",
  rider_not_allowed:
    "The rider is not an active member of the machine's organisation.",
  programs_disabled:
    "The machine takes manual sessions only, started at the machine.",
  medical_parameters_unavailable:
    "No signed medical parameters version is registered on the server, so remote launches are refused.",
  medical_parameters_unsigned:
    "The machine's medical parameters are unsigned: it is in bench mode only, with no programmed session or person on board.",
  programme_not_on_machine:
    "The programme is not, or no longer, among the machine's programmes.",
  rider_hr_max_missing:
    "The rider has neither a usable max heart rate nor a birth year.",
  zone_above_ceiling:
    "The programme's zone exceeds the current medical ceiling for the rider's max heart rate.",
  hard_max_above_hr_max:
    "The programme's hard maximum is above the rider's max heart rate.",
  rider_birth_year_missing: "The rider's birth year is not set.",
  rider_too_young:
    "The rider is younger than the minimum age in the current medical parameters version.",
  invalid_duration: "A duration is a positive number of seconds.",

  // Sessions.
  session_not_found:
    "The session does not exist, or belongs to another organisation.",
  session_not_managed: "The caller may not stop this session.",

  // The rider's feedback after a session (convex/sessionFeedback.ts, ANH-146).
  // No refusal names a value given.
  feedback_rider_only:
    "Only the session's rider gives or changes its feedback from the site.",
  feedback_not_eligible:
    "The session had no one on board (bench session, or the retired recording mode): it takes no feedback.",
  feedback_session_not_ended:
    "The session is not over: its feedback opens at its end.",
  feedback_window_closed:
    "More than EDIT_WINDOW_MS (24 h) have passed since the session's end: its feedback can no longer be given or changed.",
  feedback_invalid:
    "The feedback is not of the scale: whole scores from 0 to 10, a comfort of the list, 500 characters of text at most.",

  // The register of released versions (convex/softwareReleases.ts).
  invalid_release_version: "The version is not <component>-X.Y.Z.",
  validation_level_required: "A Pi version needs a validation level.",
  validation_level_pi_only: "Only a Pi version carries a validation level.",
  invalid_release_date: "The release date is not a positive number of ms.",
  release_notes_too_long: "The notes are longer than MAX_NOTES_LENGTH.",
  level_change_needs_notes:
    "Changing the validation level of a recorded version needs notes.",

  // The machine registry (convex/machineRegistry.ts, ANH-147).
  invalid_registry_field: "A machine registry field has an invalid value.",
  contact_not_in_organization:
    "A contact is not an active member of the machine's organisation.",
  unknown_software_version: "The Pi software version is not registered.",
  unknown_medical_parameters_version:
    "The medical parameters version is not registered.",
  invalid_registry_file:
    "The attachment is missing, invalid, too old or already referenced.",
  validation_level_not_next:
    "A review must grant exactly the next validation level.",
  validation_level_not_lower:
    "The target validation level must be lower than the current level.",
  downgrade_reason_required:
    "Lowering validation requires a reason of 3 to 500 characters.",
  occupied_requires_signed_medical_parameters:
    "Occupied validation requires a production medical key and a medical parameters version.",
  machine_validation_insufficient:
    "The machine's validation level does not permit this configuration.",

  // Alerts to the gestionnaires (convex/notifications.ts).
  notification_not_found:
    "The notification does not exist, or is not the caller's in this organisation.",
} as const satisfies Record<string, string>;

/** A stable error code of the dashboard's functions. */
export type DashboardErrorCode = keyof typeof DASHBOARD_ERRORS;

export const DASHBOARD_ERROR_CODES = Object.keys(
  DASHBOARD_ERRORS,
) as DashboardErrorCode[];

/**
 * The refusal to throw: `throw refusal("machine_not_found")`. Its data is the
 * code alone, which Convex sends to the browser as it is.
 */
export function refusal(code: DashboardErrorCode): ConvexError<string> {
  return new ConvexError<string>(code);
}
