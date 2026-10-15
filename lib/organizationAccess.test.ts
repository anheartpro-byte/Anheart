import { describe, expect, it } from "vitest";
import {
  isAnheartAdmin,
  isOrganizationAdmin,
  isPatient,
  isStaff,
  navigationFor,
  organizationView,
  type AccountAccess,
} from "./organizationAccess";

const ROLES = ["admin", "org_admin", "gestionnaire", "user"] as const;
// A role a newer server could send, and an account not loaded yet.
const UNKNOWN = ["owner", "", undefined] as const;

describe("ANH-187 what each role is", () => {
  it.each([
    { role: "admin", anheart: true, admin: true, staff: true, patient: false },
    { role: "org_admin", anheart: false, admin: true, staff: true, patient: false },
    { role: "gestionnaire", anheart: false, admin: false, staff: true, patient: false },
    { role: "user", anheart: false, admin: false, staff: false, patient: true },
  ])("$role", ({ role, anheart, admin, staff, patient }) => {
    expect(isAnheartAdmin(role)).toBe(anheart);
    expect(isOrganizationAdmin(role)).toBe(admin);
    expect(isStaff(role)).toBe(staff);
    expect(isPatient(role)).toBe(patient);
  });

  it.each(UNKNOWN)("a role the site does not know (%s) opens nothing", (role) => {
    expect(isAnheartAdmin(role)).toBe(false);
    expect(isOrganizationAdmin(role)).toBe(false);
    expect(isStaff(role)).toBe(false);
    expect(isPatient(role)).toBe(false);
  });
});

const granted = (role: string): AccountAccess => ({
  role,
  organizationAccess: "granted",
  organizationClaim: "org_centre_a",
});

const MANAGEMENT = [
  "patients",
  "machines",
  "myMachines",
  "sessions",
  "reports",
  "notifications",
];

describe("ANH-187 the sidebar of each role", () => {
  it("gives the Anheart admin administration, with the settings, and management", () => {
    expect(navigationFor(granted("admin"))).toEqual({
      administration: ["gestionnaires", "members", "users", "settings"],
      management: MANAGEMENT,
      myHealth: [],
    });
  });

  it("gives an organisation admin administration without the settings, and management", () => {
    expect(navigationFor(granted("org_admin"))).toEqual({
      administration: ["gestionnaires", "members", "users"],
      management: MANAGEMENT,
      myHealth: [],
    });
  });

  it("gives a gestionnaire management only", () => {
    expect(navigationFor(granted("gestionnaire"))).toEqual({
      administration: [],
      management: MANAGEMENT,
      myHealth: [],
    });
  });

  it("gives a patient their own machines, sessions and reports", () => {
    expect(navigationFor(granted("user"))).toEqual({
      administration: [],
      management: [],
      myHealth: ["myMachines", "sessions", "reports"],
    });
  });

  it.each(["owner", ""])("gives a role it does not know (%s) nothing", (role) => {
    expect(navigationFor(granted(role))).toEqual({
      administration: [],
      management: [],
      myHealth: [],
    });
  });

  it.each([undefined, null])("gives nothing while there is no account (%s)", (account) => {
    expect(navigationFor(account)).toEqual({
      administration: [],
      management: [],
      myHealth: [],
    });
  });

  it("gives nothing when the server accepted no organisation, whatever the role says", () => {
    // The server answers role "user" then; even a stale "admin" opens nothing.
    for (const role of ROLES) {
      expect(
        navigationFor({
          role,
          organizationAccess: "no_active_organization",
          organizationClaim: null,
        }),
      ).toEqual({ administration: [], management: [], myHealth: [] });
    }
  });
});

describe("ANH-187 whether the dashboard may draw the active organisation", () => {
  const clerk = (activeOrganization: string | null | undefined, isLoaded = true) => ({
    isLoaded,
    activeOrganization,
  });

  it("waits for Clerk, then for the account", () => {
    expect(organizationView(granted("admin"), clerk("org_centre_a", false))).toEqual({
      state: "loading",
    });
    expect(organizationView(undefined, clerk("org_centre_a"))).toEqual({
      state: "loading",
    });
  });

  it("lets the pages say themselves that nobody is signed in", () => {
    expect(organizationView(null, clerk(undefined))).toEqual({
      state: "signed-out",
    });
  });

  it("draws the answers made for the organisation Clerk shows", () => {
    expect(organizationView(granted("gestionnaire"), clerk("org_centre_a"))).toEqual({
      state: "ready",
    });
  });

  it("does not draw the previous organisation's answers while the switch reaches the server", () => {
    // Clerk already shows centre B; the account was read with centre A's token.
    expect(organizationView(granted("org_admin"), clerk("org_centre_b"))).toEqual({
      state: "switching",
    });
    // Leaving every organisation is a switch too.
    expect(organizationView(granted("org_admin"), clerk(null))).toEqual({
      state: "switching",
    });
  });

  it("draws a deployment without organisations as before: no active organisation, no claim", () => {
    const legacy: AccountAccess = {
      role: "admin",
      organizationAccess: "granted",
      organizationClaim: null,
    };
    expect(organizationView(legacy, clerk(undefined))).toEqual({ state: "ready" });
    expect(organizationView(legacy, clerk(null))).toEqual({ state: "ready" });
  });

  it("says the claim is unreadable instead of waiting for a switch that never comes", () => {
    // The server reports a malformed `org_id` as no claim at all, while Clerk
    // has an active organisation: the two never match.
    expect(
      organizationView(
        {
          role: "user",
          organizationAccess: "malformed_organization_claim",
          organizationClaim: null,
        },
        clerk("org_centre_a"),
      ),
    ).toEqual({ state: "refused", reason: "malformed_organization_claim" });
  });

  it("says why the server refused the organisation the token named", () => {
    expect(
      organizationView(
        {
          role: "user",
          organizationAccess: "organization_not_synchronized",
          organizationClaim: "org_new",
        },
        clerk("org_new"),
      ),
    ).toEqual({ state: "refused", reason: "organization_not_synchronized" });
    expect(
      organizationView(
        {
          role: "user",
          organizationAccess: "no_active_organization",
          organizationClaim: null,
        },
        clerk(null),
      ),
    ).toEqual({ state: "refused", reason: "no_active_organization" });
  });
});
