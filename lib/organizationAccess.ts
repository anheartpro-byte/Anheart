/**
 * What the dashboard shows for a role, and whether it may show the data of
 * the organisation the visitor works in.
 *
 * The rules of access are Convex's (`convex/lib/auth.ts`): the server refuses
 * what a role may not read whatever the site shows. These functions only keep
 * the site from offering what the server would refuse, and from drawing the
 * data of one organisation under the name of another.
 *
 * A role the site does not know (a newer server, or an account not loaded
 * yet) answers false everywhere: it never opens more than a patient sees.
 */

/** The Anheart admin: every organisation, machine creation, roles. */
export function isAnheartAdmin(role: string | undefined): boolean {
  return role === "admin";
}

/** An admin of the organisation worked in: the Anheart admin, or a client's. */
export function isOrganizationAdmin(role: string | undefined): boolean {
  return role === "admin" || role === "org_admin";
}

/** Whoever manages machines and patients: the admins and the gestionnaires. */
export function isStaff(role: string | undefined): boolean {
  return isOrganizationAdmin(role) || role === "gestionnaire";
}

/** A patient (role "user"). */
export function isPatient(role: string | undefined): boolean {
  return role === "user";
}

/** Why the server accepted no organisation for the call (`users.getCurrentUser`). */
export type OrganizationRefusal =
  | "no_active_organization"
  | "organization_not_synchronized"
  | "unknown_organization_role"
  | "malformed_organization_claim"
  | "no_organization";

/** What `users.getCurrentUser` says of the call, as far as these rules read it. */
export type AccountAccess = {
  role: string;
  organizationAccess: "granted" | OrganizationRefusal;
  /** The Clerk organisation the call named, accepted or not. */
  organizationClaim: string | null;
};

/** The entries of each section of the sidebar, by key of the `nav` messages. */
export type Navigation = {
  administration: string[];
  management: string[];
  myHealth: string[];
};

const NO_NAVIGATION: Navigation = {
  administration: [],
  management: [],
  myHealth: [],
};

/**
 * The sidebar of an account, under the "Tableau de bord" entry everyone has.
 *
 * Nothing but that entry while the account is loading, or when the server
 * accepted no organisation for the call: every page would be refused.
 * - Anheart admin: administration (gestionnaires, members, users, settings)
 *   and management.
 * - Organisation admin: the same without the settings, which are Anheart's.
 * - Gestionnaire: management, with the alerts of the machines they manage.
 * - Patient: their machines, sessions and reports.
 */
export function navigationFor(
  account: AccountAccess | null | undefined,
): Navigation {
  if (!account || account.organizationAccess !== "granted") {
    return NO_NAVIGATION;
  }
  const { role } = account;
  const administration: string[] = [];
  if (isOrganizationAdmin(role)) {
    administration.push("gestionnaires", "members", "users", "audit");
  }
  if (isAnheartAdmin(role)) administration.push("settings");
  return {
    administration,
    management: isStaff(role)
      ? [
          "patients",
          "machines",
          "myMachines",
          "sessions",
          "reports",
          "notifications",
        ]
      : [],
    myHealth: isPatient(role) ? ["myMachines", "sessions", "reports"] : [],
  };
}

/**
 * What the dashboard may draw in the organisation Clerk shows as active.
 *
 * - "loading": Clerk or the account is not known yet.
 * - "signed-out": no account (the pages say it themselves).
 * - "switching": Clerk shows another organisation than the one this answer
 *   was made for. The Convex client is still on the previous token: what it
 *   holds is the previous organisation's, and is not drawn.
 * - a refusal: the server accepted no organisation for this call.
 * - "ready": the answers are those of the active organisation.
 *
 * `activeOrganization` is Clerk's active organisation identifier: null or
 * undefined when the visitor has none.
 */
export type OrganizationView =
  | { state: "loading" }
  | { state: "signed-out" }
  | { state: "switching" }
  | { state: "refused"; reason: OrganizationRefusal }
  | { state: "ready" };

export function organizationView(
  account: AccountAccess | null | undefined,
  clerk: { isLoaded: boolean; activeOrganization: string | null | undefined },
): OrganizationView {
  if (!clerk.isLoaded || account === undefined) return { state: "loading" };
  if (account === null) return { state: "signed-out" };
  // An `org_id` the server cannot read comes back as no claim at all: it can
  // never match Clerk's organisation, so it is a refusal, not a switch.
  if (account.organizationAccess === "malformed_organization_claim") {
    return { state: "refused", reason: account.organizationAccess };
  }
  if ((clerk.activeOrganization ?? null) !== account.organizationClaim) {
    return { state: "switching" };
  }
  if (account.organizationAccess !== "granted") {
    return { state: "refused", reason: account.organizationAccess };
  }
  return { state: "ready" };
}
