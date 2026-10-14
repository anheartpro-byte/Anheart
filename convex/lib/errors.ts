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

  // Accounts, patients and gestionnaires.
  user_not_found: "The account does not exist (any more).",
  user_not_managed: "The caller does not manage this account.",
  patient_not_managed: "The caller does not manage this patient.",
  cannot_delete_self: "An account cannot delete itself.",
  only_patients_deletable: "A gestionnaire deletes patient accounts only.",
  not_a_patient:
    "The account is not a patient of the organisation the link would live in.",
  gestionnaire_not_found: "The gestionnaire account does not exist here.",
  not_a_gestionnaire:
    "The account is not a gestionnaire of the organisation concerned.",
  gestionnaire_self_only:
    "A gestionnaire may only add or remove themselves.",
  gestionnaire_already_assigned: "The gestionnaire already has this link.",
  gestionnaire_not_assigned: "The gestionnaire has no such link.",

  // Machines.
  machine_not_found:
    "The machine does not exist, belongs to another organisation, or was deleted.",
  machine_not_managed: "The caller does not manage this machine.",
  machine_in_session: "The machine is in a session: it cannot be deleted.",
  machine_has_pending_session:
    "A session waits for this machine: it cannot be deleted before it is cancelled.",
  machine_not_deleted: "The machine is not deleted: there is nothing to restore.",
  last_gestionnaire:
    "The caller is the machine's only gestionnaire: they cannot leave it.",

  // Patient invitations.
  invalid_invitation: "The name or the e-mail address of the invitation is not valid.",
  invitation_unavailable:
    "The invitation cannot be created for this caller and this address.",
  invitation_failed: "Clerk did not confirm that the invitation was sent.",
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
