import { messages, render, type Screen } from "@/test-support/render";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { answer, resetConvex } from "@/test-support/convex";
import { OrganizationGate } from "./OrganizationGate";

/**
 * ANH-187: the pages of the dashboard are drawn for the organisation Clerk
 * shows as active, and only once the server has answered for it. The rule is
 * `organizationView` (lib/organizationAccess.ts, with its own tests): what is
 * proven here is what the visitor sees in each case.
 */

vi.mock(
  "convex/react",
  async () => (await import("@/test-support/convex")).convexReact,
);
const clerk = vi.hoisted(() => ({
  isLoaded: true,
  orgId: "org_centre_a" as string | null | undefined,
}));
vi.mock("@clerk/nextjs", () => ({
  useAuth: () => ({ isLoaded: clerk.isLoaded, orgId: clerk.orgId }),
  OrganizationSwitcher: (props: {
    hidePersonal?: boolean;
    afterSelectOrganizationUrl?: string;
  }) => (
    <div
      data-organization-switcher=""
      data-hide-personal={props.hidePersonal}
      data-after-select={props.afterSelectOrganizationUrl}
    />
  ),
}));

const t = messages.fr.organization;

function account(organizationAccess: string, organizationClaim: string | null) {
  answer("users:getCurrentUser", {
    role: organizationAccess === "granted" ? "gestionnaire" : "user",
    organizationAccess,
    organizationClaim,
  });
}

const open = () => render(<OrganizationGate>La page</OrganizationGate>);
const switchers = (screen: Screen) =>
  screen.all((element) => element.hasAttribute("data-organization-switcher"));

beforeEach(() => {
  resetConvex();
  clerk.isLoaded = true;
  clerk.orgId = "org_centre_a";
});

describe("ANH-187 organisation gate", () => {
  it("draws the page once the server has answered for the organisation Clerk shows", () => {
    account("granted", "org_centre_a");

    expect(open().text()).toBe("La page");
  });

  it("lets the page draw its own loading state while the account or Clerk is loading", () => {
    answer("users:getCurrentUser", undefined);
    expect(open().text()).toBe("La page");

    account("granted", "org_centre_a");
    clerk.isLoaded = false;
    expect(open().text()).toContain("La page");
  });

  it("draws nothing of the previous organisation while the switch reaches the server", () => {
    // Clerk shows centre B; the account was read with centre A's token.
    account("granted", "org_centre_a");
    clerk.orgId = "org_centre_b";

    const screen = open();

    expect(screen.text()).not.toContain("La page");
    expect(screen.text()).toContain(t.switching);
    const [status] = screen.all((e) => e.getAttribute("role") === "status");
    expect(status).toBeDefined();
  });

  it.each([
    "no_active_organization",
    "organization_not_synchronized",
    "unknown_organization_role",
    "malformed_organization_claim",
    "no_organization",
  ] as const)(
    "says why the server refused the organisation (%s), draws nothing of the page, and offers to choose one",
    (reason) => {
      clerk.orgId = reason === "no_active_organization" ? null : "org_x";
      // The server reads a malformed `org_id` as no claim.
      account(reason, reason === "malformed_organization_claim" ? null : clerk.orgId);

      const screen = open();

      expect(screen.text()).not.toContain("La page");
      const [alert] = screen.all((e) => e.getAttribute("role") === "alert");
      expect(screen.textOf(alert)).toBe(t.refused[reason]);
      const [switcher] = switchers(screen);
      expect(switcher?.getAttribute("data-hide-personal")).toBe("true");
      expect(switcher?.getAttribute("data-after-select")).toBe("/fr/dashboard");
    },
  );

  it("draws a deployment without organisations as before", () => {
    clerk.orgId = undefined;
    account("granted", null);

    const screen = open();

    expect(screen.text()).toBe("La page");
    expect(switchers(screen)).toEqual([]);
  });
});
