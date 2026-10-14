/// <reference types="vite/client" />
/**
 * ANH-187: what the site reads to work in one organisation at a time. The
 * account says whether the server accepted the organisation of the call, and
 * why not; which organisation the call named; whether roles are now changed
 * in Clerk only. The Anheart admin lists the organisations a machine can be
 * created in, and the gestionnaires of one of them.
 */
import { afterEach, describe, expect, it } from "vitest";
import { api, internal } from "./_generated/api";
import {
  ORG_A_CLERK_ORG,
  ORG_B_CLERK_ORG,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLegacyWorld,
  seedWorld,
} from "./test.setup";

afterEach(() => {
  configureAnheartOrganization(null);
});

describe("the account says how the server took the organisation of the call", () => {
  it("is granted, names the organisation of the token, and roles are Clerk's once ANHEART_ORG_ID is set", async () => {
    const w = await seedWorld(modules);

    const me = await as(w.t, "orgAdmin").query(api.users.getCurrentUser, {});

    expect(me).toMatchObject({
      role: "org_admin",
      organization: { _id: w.orgA },
      organizationAccess: "granted",
      organizationClaim: ORG_A_CLERK_ORG,
      rolesManagedInClerk: true,
    });
  });

  it.each([
    {
      why: "no active organisation",
      claims: {},
      access: "no_active_organization",
      claim: null,
    },
    {
      why: "an organisation the webhook has not created yet",
      claims: { org_id: "org_not_yet_mirrored", org_role: "org:admin" },
      access: "organization_not_synchronized",
      claim: "org_not_yet_mirrored",
    },
    {
      why: "a role Convex does not know",
      claims: { org_id: ORG_B_CLERK_ORG, org_role: "org:member" },
      access: "unknown_organization_role",
      claim: ORG_B_CLERK_ORG,
    },
    {
      why: "an organisation claim that is not a text",
      claims: { org_id: 42, org_role: "org:admin" },
      access: "malformed_organization_claim",
      claim: null,
    },
  ])("names the refusal for $why, and opens nothing", async ({ claims, access, claim }) => {
    const w = await seedWorld(modules);

    const me = await w.t
      .withIdentity({ subject: "orgBAdmin", ...claims })
      .query(api.users.getCurrentUser, {});

    expect(me).toMatchObject({
      _id: w.orgBAdmin,
      role: "user",
      organization: null,
      organizationAccess: access,
      organizationClaim: claim,
    });
  });

  it("before ANHEART_ORG_ID: the stored membership is granted, without a claim, and roles change on the site", async () => {
    const w = await seedLegacyWorld(modules);
    await w.t.mutation(
      internal.migrations.multiOrganization.attachExistingRowsToAnheart,
      {},
    );

    const me = await w.t
      .withIdentity({ subject: "legacy-admin" })
      .query(api.users.getCurrentUser, {});

    expect(me).toMatchObject({
      role: "admin",
      organizationAccess: "granted",
      organizationClaim: null,
      rolesManagedInClerk: false,
    });
  });

  it("before ANHEART_ORG_ID: an account without a membership is told it belongs to no organisation", async () => {
    const w = await seedLegacyWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.insert("users", {
        clerkId: "no-membership",
        role: "user",
        firstName: "No",
        lastName: "Membership",
        email: "no-membership@example.invalid",
        language: "fr",
        createdAt: NOW,
      }),
    );

    const me = await w.t
      .withIdentity({ subject: "no-membership" })
      .query(api.users.getCurrentUser, {});

    expect(me).toMatchObject({
      organization: null,
      organizationAccess: "no_organization",
      organizationClaim: null,
    });
  });
});

describe("the organisations a machine can be created in", () => {
  it("are every organisation still in Clerk, by name, for the Anheart admin", async () => {
    const w = await seedWorld(modules);
    const gone = await w.t.run((ctx) =>
      ctx.db.insert("organizations", {
        clerkOrgId: "org_deleted",
        name: "Aaa deleted",
        slug: "deleted",
        createdAt: NOW,
        deletedAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      }),
    );

    const listed = await as(w.t, "admin").query(
      api.organizations.listOrganizations,
      {},
    );

    expect(listed).toEqual([
      { _id: w.anheartOrg, name: "Anheart", slug: "anheart" },
      { _id: w.orgA, name: "Centre A", slug: "centre-a" },
      { _id: w.orgB, name: "Centre B", slug: "centre-b" },
    ]);
    expect(listed.map((o) => o._id)).not.toContain(gone);
  });

  it("never take a new machine once deleted in Clerk", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.orgB, { deletedAt: NOW }));

    await expect(
      as(w.t, "admin").mutation(api.machines.createMachine, {
        name: "Centri Lyon",
        organizationId: w.orgB,
      }),
    ).rejects.toThrow("Organization not found");
    const inB = await w.t.run((ctx) =>
      ctx.db
        .query("machines")
        .withIndex("by_organization", (q) => q.eq("organizationId", w.orgB))
        .collect(),
    );
    expect(inB.map((m) => m.name)).not.toContain("Centri Lyon");
  });

  it("are none for an organisation admin, who creates no machine", async () => {
    const w = await seedWorld(modules);

    expect(
      await as(w.t, "orgAdmin").query(api.organizations.listOrganizations, {}),
    ).toEqual([]);
  });
});

describe("the gestionnaires of one organisation", () => {
  /** `manager` of centre A is also a gestionnaire of centre B, with a machine there. */
  async function managerInBothCentres() {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.insert("memberships", {
        userId: w.manager,
        organizationId: w.orgB,
        role: "gestionnaire",
        active: true,
      });
      await ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgB,
        machineId: w.orgBMachine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      });
    });
    return w;
  }

  const byName = (rows: { firstName: string; machineCount: number }[]) =>
    rows
      .map((row) => [row.firstName, row.machineCount] as const)
      .sort(([a], [b]) => a.localeCompare(b));

  it("are those of the named organisation for the Anheart admin, counted in it", async () => {
    const w = await managerInBothCentres();
    const admin = as(w.t, "admin");

    const inA = await admin.query(api.users.listGestionnaires, {
      organizationId: w.orgA,
    });
    const inB = await admin.query(api.users.listGestionnaires, {
      organizationId: w.orgB,
    });
    const everywhere = await admin.query(api.users.listGestionnaires, {});

    expect(byName(inA)).toEqual([
      ["manager", 1],
      ["otherManager", 1],
    ]);
    expect(byName(inB)).toEqual([
      ["manager", 1],
      ["orgBManager", 1],
    ]);
    // Without an organisation: every link, of every organisation.
    expect(byName(everywhere)).toEqual([
      ["manager", 2],
      ["orgBManager", 1],
      ["otherManager", 1],
    ]);
  });

  it("are the organisation admin's own when they name their organisation, and none for another", async () => {
    const w = await managerInBothCentres();
    const orgAdmin = as(w.t, "orgAdmin");

    const own = await orgAdmin.query(api.users.listGestionnaires, {
      organizationId: w.orgA,
    });
    const foreign = await orgAdmin.query(api.users.listGestionnaires, {
      organizationId: w.orgB,
    });

    expect(byName(own)).toEqual([
      ["manager", 1],
      ["otherManager", 1],
    ]);
    expect(foreign).toEqual([]);
  });
});
