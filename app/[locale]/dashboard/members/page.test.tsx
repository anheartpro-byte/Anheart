// @vitest-environment jsdom
import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "@/convex/_generated/api";
import fr from "@/messages/fr.json";
import {
  answer,
  propsOf,
  renderPage,
  signedInAs,
  wasMounted,
} from "@/test-support/pages";
import MembersPage from "./page";

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/pages")).convexReact,
);
vi.mock(
  "@/i18n/navigation",
  async () => (await import("@/test-support/pages")).navigation,
);
const clerk = vi.hoisted(() => ({
  isLoaded: true,
  orgId: "org_centre_a" as string | null | undefined,
}));
// Clerk draws the members and applies its own rights: the page only places it.
vi.mock("@clerk/nextjs", async () => {
  const { standIn } = await import("@/test-support/pages");
  return {
    useAuth: () => ({ isLoaded: clerk.isLoaded, orgId: clerk.orgId }),
    OrganizationProfile: standIn("OrganizationProfile"),
    OrganizationSwitcher: standIn("OrganizationSwitcher"),
  };
});

/** ANH-187: the members of the active organisation, managed in Clerk. */

beforeEach(() => {
  clerk.isLoaded = true;
  clerk.orgId = "org_centre_a";
});

describe("members page: who may read it", () => {
  it("shows nothing until it knows who the visitor is and Clerk is loaded", async () => {
    answer(api.users.getCurrentUser, undefined);
    const view = await renderPage(<MembersPage />);
    expect(screen.queryByRole("heading")).toBeNull();

    signedInAs("org_admin");
    clerk.isLoaded = false;
    await view.refresh();
    expect(screen.queryByRole("heading")).toBeNull();
    expect(wasMounted("OrganizationProfile")).toBe(false);
  });

  it.each([
    ["a manager", "gestionnaire"],
    ["a patient", "user"],
  ] as const)("refuses %s, and shows no member", async (_name, role) => {
    signedInAs(role);
    await renderPage(<MembersPage />);

    expect(screen.getByText(fr.members.accessDenied)).toBeTruthy();
    expect(wasMounted("OrganizationProfile")).toBe(false);
  });

  it("refuses an account without a row", async () => {
    answer(api.users.getCurrentUser, null);
    await renderPage(<MembersPage />);

    expect(screen.getByText(fr.members.accessDenied)).toBeTruthy();
  });

  it("refuses an admin whose organisation the server did not accept", async () => {
    signedInAs("admin", { organizationAccess: "organization_not_synchronized" });
    await renderPage(<MembersPage />);

    expect(screen.getByText(fr.members.accessDenied)).toBeTruthy();
    expect(wasMounted("OrganizationProfile")).toBe(false);
  });
});

describe("members page: what an admin sees", () => {
  it.each([
    ["the Anheart admin", "admin"],
    ["the admin of a client organisation", "org_admin"],
  ] as const)(
    "shows %s the members of the active organisation, in Clerk's panel",
    async (_name, role) => {
      signedInAs(role);
      await renderPage(<MembersPage />);

      expect(
        screen.getByRole("heading", { level: 1, name: fr.members.title }),
      ).toBeTruthy();
      expect(screen.getByText(fr.members.description)).toBeTruthy();
      expect(propsOf("OrganizationProfile")).toEqual({
        routing: "hash",
        afterLeaveOrganizationUrl: "/fr/dashboard",
      });
    },
  );

  it("asks first for an organisation when Clerk has none active", async () => {
    // A deployment before ANHEART_ORG_ID: the account is granted by its
    // stored membership, and Clerk has no active organisation.
    clerk.orgId = null;
    signedInAs("admin");
    await renderPage(<MembersPage />);

    expect(wasMounted("OrganizationProfile")).toBe(false);
    expect(propsOf("OrganizationSwitcher")).toEqual({
      hidePersonal: true,
      afterSelectOrganizationUrl: "/fr/dashboard/members",
    });
  });
});
