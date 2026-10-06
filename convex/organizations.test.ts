/// <reference types="vite/client" />
/**
 * ANH-114 EX-2: the organisation and the role of a call come from the verified
 * identity, never from a field a user can change. And the transition: what a
 * token WITHOUT organisation claims is allowed to do, before and after Clerk
 * Organizations is configured for the deployment.
 */
import { afterEach, describe, expect, it } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import {
  effectiveRole,
  inScope,
  isAnheartOrganization,
  organizationRoleFromClaim,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";
import {
  ANHEART_CLERK_ORG,
  ORG_A_CLERK_ORG,
  ORG_B_CLERK_ORG,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLegacyWorld,
  seedWorld,
  type LegacyWorld,
  type World,
} from "./test.setup";

afterEach(() => {
  configureAnheartOrganization(null);
});

/** What the site reads to know who is calling. */
const whoAmI = (reader: ReturnType<World["t"]["withIdentity"]>) =>
  reader.query(api.users.getCurrentUser, {});

describe("EX-2 the role and the organisation are those of the verified identity", () => {
  it("acts with the role the token carries in the organisation the token names", async () => {
    const w = await seedWorld(modules);
    // The same account is a gestionnaire of centre A and a patient of centre B.
    await w.t.run((ctx) =>
      ctx.db.insert("memberships", {
        userId: w.manager,
        organizationId: w.orgB,
        role: "user",
        active: true,
      }),
    );

    const inA = await whoAmI(
      w.t.withIdentity({
        subject: "manager",
        org_id: ORG_A_CLERK_ORG,
        org_role: "org:gestionnaire",
      }),
    );
    const inB = await whoAmI(
      w.t.withIdentity({
        subject: "manager",
        org_id: ORG_B_CLERK_ORG,
        org_role: "org:patient",
      }),
    );

    expect(inA).toMatchObject({
      role: "gestionnaire",
      organization: { _id: w.orgA, name: "Centre A", slug: "centre-a" },
    });
    expect(inB).toMatchObject({ role: "user", organization: { _id: w.orgB } });
    // In centre B the account is a patient: it manages nothing there, and
    // nothing of centre A follows it.
    const asPatientOfB = w.t.withIdentity({
      subject: "manager",
      org_id: ORG_B_CLERK_ORG,
      org_role: "org:patient",
    });
    expect(await asPatientOfB.query(api.machines.listMachines, {})).toEqual([]);
    expect(
      await asPatientOfB.query(api.users.getUserById, { userId: w.patient }),
    ).toBeNull();
  });

  it("never reads the stored role to authorise the caller", async () => {
    const w = await seedWorld(modules);
    // The mirror says "admin"; the token says patient of centre A.
    await w.t.run((ctx) => ctx.db.patch(w.stranger, { role: "admin" }));

    const me = await whoAmI(as(w.t, "stranger"));

    expect(me?.role).toBe("user");
    await expect(
      as(w.t, "stranger").mutation(api.machines.createMachine, { name: "x" }),
    ).rejects.toThrow(/Unauthorized/);
    expect(
      await as(w.t, "stranger").query(api.machines.listMachines, {}),
    ).toEqual([]);
  });

  it("never reads the stored main organisation when the token names one", async () => {
    const w = await seedWorld(modules);
    // The row says centre B; the token says gestionnaire of centre A.
    await w.t.run((ctx) => ctx.db.patch(w.manager, { organizationId: w.orgB }));

    const me = await whoAmI(as(w.t, "manager"));

    expect(me?.organization?._id).toBe(w.orgA);
    expect(
      await as(w.t, "manager").query(api.machines.getMachine, {
        machineId: w.orgBMachine,
      }),
    ).toBeNull();
  });

  it("accepts the Clerk role with or without its org: prefix", async () => {
    const w = await seedWorld(modules);

    for (const org_role of ["org:gestionnaire", "gestionnaire"]) {
      const me = await whoAmI(
        w.t.withIdentity({
          subject: "manager",
          org_id: ORG_A_CLERK_ORG,
          org_role,
        }),
      );
      expect(me?.role).toBe("gestionnaire");
    }
    const patient = await whoAmI(
      w.t.withIdentity({
        subject: "patient",
        org_id: ORG_A_CLERK_ORG,
        org_role: "patient",
      }),
    );
    expect(patient?.role).toBe("user");
  });
});

describe("EX-1 only the admins of the organisation named by ANHEART_ORG_ID are Anheart admins", () => {
  it("makes an admin of the Anheart organisation the admin of every organisation", async () => {
    const w = await seedWorld(modules);

    expect((await whoAmI(as(w.t, "admin")))?.role).toBe("admin");
    const machines = await as(w.t, "admin").query(
      api.machines.listMachines,
      {},
    );
    expect(machines.map((m) => m._id).sort()).toEqual(
      [w.machine, w.otherMachine, w.orgBMachine].sort(),
    );
  });

  it("makes an admin of any other organisation the admin of that organisation only", async () => {
    const w = await seedWorld(modules);

    expect((await whoAmI(as(w.t, "orgAdmin")))?.role).toBe("org_admin");
    const machines = await as(w.t, "orgAdmin").query(
      api.machines.listMachines,
      {},
    );
    expect(machines.map((m) => m._id).sort()).toEqual(
      [w.machine, w.otherMachine].sort(),
    );
  });

  it("follows the variable, not the name or the slug of the organisation", async () => {
    const w = await seedWorld(modules);
    // The variable now names centre B: its admin is the Anheart admin, and the
    // admin of the organisation called "Anheart" is an organisation admin.
    configureAnheartOrganization(ORG_B_CLERK_ORG);

    expect((await whoAmI(as(w.t, "admin")))?.role).toBe("org_admin");
    expect((await whoAmI(as(w.t, "orgBAdmin")))?.role).toBe("admin");
    expect(await as(w.t, "admin").query(api.machines.listMachines, {})).toEqual(
      [],
    );
  });

  it("gives nobody the Anheart admin role through claims while the variable is unset", async () => {
    const w = await seedWorld(modules);
    configureAnheartOrganization(null);

    expect((await whoAmI(as(w.t, "admin")))?.role).toBe("org_admin");
    await expect(
      as(w.t, "admin").mutation(api.machines.createMachine, { name: "x" }),
    ).rejects.toThrow(/Unauthorized/);
  });

  it("treats a blank variable as unset", async () => {
    const w = await seedWorld(modules);
    process.env.ANHEART_ORG_ID = "   ";

    expect((await whoAmI(as(w.t, "admin")))?.role).toBe("org_admin");
  });
});

describe("EX-1 / EX-2 the rules themselves", () => {
  const anheart = { clerkOrgId: ANHEART_CLERK_ORG, slug: "anheart-sas" };
  const client = { clerkOrgId: ORG_A_CLERK_ORG, slug: "anheart" };
  const unlinkedDefault = { clerkOrgId: undefined, slug: "anheart" };
  const unlinkedOther = { clerkOrgId: undefined, slug: "centre" };

  it("recognises the Anheart organisation by the variable once it is set", () => {
    configureAnheartOrganization(ANHEART_CLERK_ORG);

    expect(isAnheartOrganization(anheart)).toBe(true);
    expect(isAnheartOrganization(client)).toBe(false);
    expect(isAnheartOrganization(unlinkedDefault)).toBe(false);
  });

  it("recognises only the unlinked default organisation while the variable is unset", () => {
    configureAnheartOrganization(null);

    expect(isAnheartOrganization(unlinkedDefault)).toBe(true);
    expect(isAnheartOrganization(unlinkedOther)).toBe(false);
    expect(isAnheartOrganization(anheart)).toBe(false);
    expect(isAnheartOrganization(client)).toBe(false);
  });

  it("derives the role of a call from the role held and the organisation it is held in", () => {
    configureAnheartOrganization(ANHEART_CLERK_ORG);

    expect(effectiveRole(anheart, "admin")).toBe("admin");
    expect(effectiveRole(client, "admin")).toBe("org_admin");
    // Only an admin role is ever lifted: the Anheart organisation's
    // gestionnaires and patients are ordinary ones.
    expect(effectiveRole(anheart, "gestionnaire")).toBe("gestionnaire");
    expect(effectiveRole(anheart, "user")).toBe("user");
    expect(effectiveRole(client, "user")).toBe("user");
  });

  it("maps exactly the three Clerk roles", () => {
    expect(organizationRoleFromClaim("org:admin")).toBe("admin");
    expect(organizationRoleFromClaim("org:gestionnaire")).toBe("gestionnaire");
    expect(organizationRoleFromClaim("org:patient")).toBe("user");
    for (const claim of [
      "org:member",
      "org:user",
      "user",
      "org:org:admin",
      "ORG:ADMIN",
      "org:",
      "",
      "toString",
      undefined,
      null,
      true,
      ["org:admin"],
      { role: "org:admin" },
    ]) {
      expect(organizationRoleFromClaim(claim), String(claim)).toBeNull();
    }
  });

  it("places a row in the scope of its own organisation, and of the Anheart admin", () => {
    const mine = "organization-one" as Id<"organizations">;
    const theirs = "organization-two" as Id<"organizations">;
    const caller = (role: CurrentUser["role"]) =>
      ({ role, organizationId: mine }) as CurrentUser;

    for (const role of ["org_admin", "gestionnaire", "user"] as const) {
      expect(sameOrganization(caller(role), mine)).toBe(true);
      expect(inScope(caller(role), mine)).toBe(true);
      expect(inScope(caller(role), theirs)).toBe(false);
      // A row without organisation is in nobody's scope.
      expect(sameOrganization(caller(role), undefined)).toBe(false);
      expect(inScope(caller(role), undefined)).toBe(false);
    }
    expect(inScope(caller("admin"), theirs)).toBe(true);
    expect(inScope(caller("admin"), undefined)).toBe(true);
    // "Same organisation" stays literal, even for the Anheart admin.
    expect(sameOrganization(caller("admin"), theirs)).toBe(false);
  });
});

describe("EX-2 a call the server cannot place in an organisation is refused", () => {
  const refusals: Array<{
    name: string;
    claims: Record<string, string | number | boolean | null>;
    message: RegExp;
  }> = [
    {
      name: "an organisation the server does not know",
      claims: { org_id: "org_synthetic_unknown", org_role: "org:admin" },
      message: /not known to the server/,
    },
    {
      name: "a role the server does not recognise",
      claims: { org_id: ORG_A_CLERK_ORG, org_role: "org:member" },
      message: /role in this organization is not recognized/,
    },
    {
      name: "a role inherited from Object.prototype",
      claims: { org_id: ORG_A_CLERK_ORG, org_role: "constructor" },
      message: /role in this organization is not recognized/,
    },
    {
      name: "a role that is not a string",
      claims: { org_id: ORG_A_CLERK_ORG, org_role: 1 },
      message: /role in this organization is not recognized/,
    },
    {
      name: "no role at all",
      claims: { org_id: ORG_A_CLERK_ORG },
      message: /role in this organization is not recognized/,
    },
    {
      name: "an organisation claim that is not a string",
      claims: { org_id: 42, org_role: "org:admin" },
      message: /Malformed organization claim/,
    },
    {
      name: "no organisation claim",
      claims: {},
      message: /No active organization/,
    },
    {
      name: "a null organisation claim (no active organisation in Clerk)",
      claims: { org_id: null, org_role: null },
      message: /No active organization/,
    },
    {
      name: "an empty organisation claim",
      claims: { org_id: "", org_role: "org:admin" },
      message: /No active organization/,
    },
  ];

  it.each(refusals)("refuses $name", async ({ claims, message }) => {
    const w = await seedWorld(modules);
    // `orgAdmin` is an admin of centre A in the mirror: only the token counts.
    const caller = w.t.withIdentity({ ...claims, subject: "orgAdmin" });

    // Organisation-scoped reads and writes refuse, with the reason.
    await expect(caller.query(api.machines.listMachines, {})).rejects.toThrow(
      message,
    );
    await expect(
      caller.query(api.machines.getMachine, { machineId: w.machine }),
    ).rejects.toThrow(message);
    await expect(
      caller.mutation(api.users.updateUserProfile, { firstName: "x" }),
    ).rejects.toThrow(message);
    // The site still learns who is signed in, with no organisation and the
    // least role.
    expect(await whoAmI(caller)).toMatchObject({
      _id: w.orgAdmin,
      role: "user",
      organization: null,
    });
  });

  it("returns null to an account that is signed in but not registered", async () => {
    const w = await seedWorld(modules);

    const me = await whoAmI(
      w.t.withIdentity({
        subject: "nobody-yet",
        org_id: ORG_A_CLERK_ORG,
        org_role: "org:admin",
      }),
    );

    expect(me).toBeNull();
    await expect(
      w.t
        .withIdentity({
          subject: "nobody-yet",
          org_id: ORG_A_CLERK_ORG,
          org_role: "org:admin",
        })
        .query(api.machines.listMachines, {}),
    ).rejects.toThrow(/User not found in database/);
  });
});

describe("EX-2 first sign-in mirrors the membership the token carries", () => {
  const newcomer = (w: World, org_id: string, org_role: string) =>
    w.t.withIdentity({
      subject: "newcomer",
      org_id,
      org_role,
      givenName: "New",
      familyName: "Comer",
      email: "newcomer@example.invalid",
    });

  const mirrorOf = (w: World, clerkId: string) =>
    w.t.run(async (ctx) => {
      const user = await ctx.db
        .query("users")
        .withIndex("by_clerk_id", (q) => q.eq("clerkId", clerkId))
        .unique();
      const memberships = user
        ? await ctx.db
            .query("memberships")
            .withIndex("by_user", (q) => q.eq("userId", user._id))
            .collect()
        : [];
      return {
        user,
        memberships: memberships.map((m) => ({
          organizationId: m.organizationId,
          role: m.role,
          active: m.active,
        })),
      };
    });

  it("creates the account in the organisation of the token, with its role", async () => {
    const w = await seedWorld(modules);

    const userId = await newcomer(w, ORG_B_CLERK_ORG, "org:patient").mutation(
      api.users.getOrCreateUser,
      {},
    );

    const { user, memberships } = await mirrorOf(w, "newcomer");
    expect(user).toMatchObject({
      _id: userId,
      role: "user",
      organizationId: w.orgB,
      firstName: "New",
      lastName: "Comer",
      email: "newcomer@example.invalid",
    });
    expect(memberships).toEqual([
      { organizationId: w.orgB, role: "user", active: true },
    ]);
    // Centre B's admin now lists them; centre A's does not.
    const listedInB = await as(w.t, "orgBAdmin").query(api.users.listUsers, {});
    const listedInA = await as(w.t, "orgAdmin").query(api.users.listUsers, {});
    expect(listedInB.map((u) => u._id)).toContain(userId);
    expect(listedInA.map((u) => u._id)).not.toContain(userId);
  });

  it("updates the mirror of an existing account without moving its main organisation", async () => {
    const w = await seedWorld(modules);
    // `stranger` is a patient of centre A; Clerk now also makes them a
    // gestionnaire of centre B, and their old membership there was inactive.
    await w.t.run((ctx) =>
      ctx.db.insert("memberships", {
        userId: w.stranger,
        organizationId: w.orgB,
        role: "user",
        active: false,
      }),
    );

    const userId = await w.t
      .withIdentity({
        subject: "stranger",
        org_id: ORG_B_CLERK_ORG,
        org_role: "org:gestionnaire",
      })
      .mutation(api.users.getOrCreateUser, {});

    expect(userId).toBe(w.stranger);
    const { user, memberships } = await mirrorOf(w, "stranger");
    expect(user?.organizationId).toBe(w.orgA);
    expect(user?.role).toBe("gestionnaire");
    expect(memberships).toHaveLength(2);
    expect(memberships).toContainEqual({
      organizationId: w.orgB,
      role: "gestionnaire",
      active: true,
    });
    expect(memberships).toContainEqual({
      organizationId: w.orgA,
      role: "user",
      active: true,
    });
  });

  it("adds the membership of an existing account that had none in that organisation", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.patch(w.stranger, { organizationId: undefined }),
    );

    await w.t
      .withIdentity({
        subject: "stranger",
        org_id: ORG_B_CLERK_ORG,
        org_role: "org:patient",
      })
      .mutation(api.users.getOrCreateUser, {});

    const { user, memberships } = await mirrorOf(w, "stranger");
    // An account without a main organisation takes the one of the token.
    expect(user?.organizationId).toBe(w.orgB);
    expect(memberships).toContainEqual({
      organizationId: w.orgB,
      role: "user",
      active: true,
    });
  });

  it("creates the account without organisation when the server refuses the token's", async () => {
    const w = await seedWorld(modules);

    await newcomer(w, "org_synthetic_unknown", "org:admin").mutation(
      api.users.getOrCreateUser,
      {},
    );

    const { user, memberships } = await mirrorOf(w, "newcomer");
    expect(user?.role).toBe("user");
    expect(user?.organizationId).toBeUndefined();
    expect(memberships).toEqual([]);
  });

  it("leaves an existing account untouched when the token carries no organisation", async () => {
    const w = await seedWorld(modules);

    const userId = await w.t
      .withIdentity({ subject: "stranger" })
      .mutation(api.users.getOrCreateUser, {});

    expect(userId).toBe(w.stranger);
    const { user, memberships } = await mirrorOf(w, "stranger");
    expect(user).toMatchObject({ role: "user", organizationId: w.orgA });
    expect(memberships).toEqual([
      { organizationId: w.orgA, role: "user", active: true },
    ]);
  });
});

// ---------------------------------------------------------------------------
// Transition: tokens without organisation claims.
// ---------------------------------------------------------------------------

const legacy = (w: LegacyWorld, subject: string) =>
  w.t.withIdentity({ subject });
const migrate = (w: LegacyWorld) =>
  w.t.mutation(internal.migrations.multiOrganization.attachExistingRowsToAnheart, {});

describe("transition: before Clerk Organizations is configured (ANHEART_ORG_ID unset)", () => {
  it("refuses everything organisation-scoped until the migration has run", async () => {
    const w = await seedLegacyWorld(modules);

    for (const subject of [
      "legacy-admin",
      "legacy-manager",
      "legacy-patient",
    ]) {
      await expect(
        legacy(w, subject).query(api.machines.listMachines, {}),
      ).rejects.toThrow(/does not belong to an organization/);
      expect(await whoAmI(legacy(w, subject))).toMatchObject({
        role: "user",
        organization: null,
      });
    }
  });

  it("serves the single organisation as before once the migration has run", async () => {
    const w = await seedLegacyWorld(modules);
    const { organizationId } = await migrate(w);

    // The admin of the default organisation is the Anheart admin.
    expect(await whoAmI(legacy(w, "legacy-admin"))).toMatchObject({
      role: "admin",
      organization: { _id: organizationId, name: "Anheart", slug: "anheart" },
    });
    expect(await whoAmI(legacy(w, "legacy-manager"))).toMatchObject({
      role: "gestionnaire",
    });
    // Each role reads what it read before.
    const forManager = await legacy(w, "legacy-manager").query(
      api.machines.listMachines,
      {},
    );
    expect(forManager.map((m) => m._id)).toEqual([w.machine]);
    const patients = await legacy(w, "legacy-manager").query(
      api.users.listUsers,
      {},
    );
    expect(patients.map((u) => u._id)).toEqual([w.patient]);
    const telemetry = await legacy(w, "legacy-patient").query(
      api.training.getSessionTelemetry,
      { sessionId: w.session },
    );
    expect(telemetry).toHaveLength(5);
    const sessionId = await legacy(w, "legacy-patient").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: "p1" },
    );
    const launched = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(launched?.organizationId).toBe(organizationId);
  });

  it("resolves the organisation from server-side rows, never from an argument", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    // A second organisation with a machine: the legacy gestionnaire has no
    // membership there and no argument lets them claim one.
    const foreign = await w.t.run(async (ctx) => {
      const organization = await ctx.db.insert("organizations", {
        clerkOrgId: "org_synthetic_other",
        name: "Other centre",
        slug: "other-centre",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      });
      return await ctx.db.insert("machines", {
        organizationId: organization,
        name: "Other machine",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW,
        config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
        createdAt: NOW,
      });
    });

    expect(
      await legacy(w, "legacy-manager").query(api.machines.getMachine, {
        machineId: foreign,
      }),
    ).toBeNull();
    await expect(
      legacy(w, "legacy-manager").mutation(api.machines.updateMachine, {
        machineId: foreign,
        name: "taken",
      }),
    ).rejects.toThrow(/Not authorized to manage this machine/);
  });

  it("refuses an account whose membership is not active", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    await w.t.run(async (ctx) => {
      const membership = await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", w.manager))
        .unique();
      await ctx.db.patch(membership!._id, { active: false });
    });

    await expect(
      legacy(w, "legacy-manager").query(api.machines.listMachines, {}),
    ).rejects.toThrow(/does not belong to an organization/);
    expect(await whoAmI(legacy(w, "legacy-manager"))).toMatchObject({
      organization: null,
    });
  });

  it("refuses an account whose main organisation no longer exists", async () => {
    const w = await seedLegacyWorld(modules);
    const { organizationId } = await migrate(w);
    await w.t.run((ctx) => ctx.db.delete(organizationId));

    await expect(
      legacy(w, "legacy-manager").query(api.machines.listMachines, {}),
    ).rejects.toThrow(/does not belong to an organization/);
  });

  it("lets the admin change a role, in the membership and in its mirror", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);

    await legacy(w, "legacy-admin").mutation(api.users.updateUserRole, {
      userId: w.patient,
      role: "gestionnaire",
    });

    const { user, membership } = await w.t.run(async (ctx) => ({
      user: await ctx.db.get(w.patient),
      membership: await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", w.patient))
        .unique(),
    }));
    expect(user?.role).toBe("gestionnaire");
    expect(membership).toMatchObject({ role: "gestionnaire", active: true });
    expect((await whoAmI(legacy(w, "legacy-patient")))?.role).toBe(
      "gestionnaire",
    );
  });

  it("refuses to change the role of an account outside the admin's organisation", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    const outsider = await w.t.run((ctx) =>
      ctx.db.insert("users", {
        clerkId: "outsider",
        role: "user" as const,
        firstName: "Out",
        lastName: "Sider",
        email: "outsider@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      }),
    );

    await expect(
      legacy(w, "legacy-admin").mutation(api.users.updateUserRole, {
        userId: outsider,
        role: "admin",
      }),
    ).rejects.toThrow(/User not found/);
    expect((await w.t.run((ctx) => ctx.db.get(outsider)))?.role).toBe("user");
  });

  it("puts a new sign-up in the default organisation as a plain user", async () => {
    const w = await seedLegacyWorld(modules);
    const { organizationId } = await migrate(w);

    const userId = await legacy(w, "fresh-signup").mutation(
      api.users.getOrCreateUser,
      {},
    );

    expect(await whoAmI(legacy(w, "fresh-signup"))).toMatchObject({
      _id: userId,
      role: "user",
      organization: { _id: organizationId },
    });
    // As before: a plain user sees themselves and no machine.
    const users = await legacy(w, "fresh-signup").query(
      api.users.listUsers,
      {},
    );
    expect(users.map((u) => u._id)).toEqual([userId]);
    expect(
      await legacy(w, "fresh-signup").query(api.machines.listMachines, {}),
    ).toEqual([]);
  });

  it("creates a new sign-up without organisation before the migration", async () => {
    const w = await seedLegacyWorld(modules);

    const userId = await legacy(w, "fresh-signup").mutation(
      api.users.getOrCreateUser,
      {},
    );

    const user = await w.t.run((ctx) => ctx.db.get(userId));
    expect(user?.organizationId).toBeUndefined();
    expect(await whoAmI(legacy(w, "fresh-signup"))).toMatchObject({
      organization: null,
    });
    // The migration then attaches it like every other account.
    await migrate(w);
    expect((await whoAmI(legacy(w, "fresh-signup")))?.role).toBe("user");
  });
});

describe("transition: once Clerk Organizations is configured (ANHEART_ORG_ID set)", () => {
  it("refuses a token without organisation claims, whatever the mirror says", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    expect((await whoAmI(legacy(w, "legacy-admin")))?.role).toBe("admin");

    configureAnheartOrganization(ANHEART_CLERK_ORG);

    for (const subject of [
      "legacy-admin",
      "legacy-manager",
      "legacy-patient",
    ]) {
      await expect(
        legacy(w, subject).query(api.machines.listMachines, {}),
      ).rejects.toThrow(/No active organization/);
      expect(await whoAmI(legacy(w, subject))).toMatchObject({
        role: "user",
        organization: null,
      });
    }
  });

  it("no longer lets anyone change a role outside Clerk", async () => {
    const w = await seedLegacyWorld(modules);
    configureAnheartOrganization(ANHEART_CLERK_ORG);
    await migrate(w);
    const admin = w.t.withIdentity({
      subject: "legacy-admin",
      org_id: ANHEART_CLERK_ORG,
      org_role: "org:admin",
    });
    expect((await whoAmI(admin))?.role).toBe("admin");

    await expect(
      admin.mutation(api.users.updateUserRole, {
        userId: w.patient,
        role: "admin",
      }),
    ).rejects.toThrow(/Roles are managed in Clerk Organizations/);
    expect((await w.t.run((ctx) => ctx.db.get(w.patient)))?.role).toBe("user");
  });

  it("serves the linked default organisation to tokens that carry its claims", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    const claims = { org_id: ANHEART_CLERK_ORG, org_role: "org:admin" };
    configureAnheartOrganization(ANHEART_CLERK_ORG);

    // Not linked yet: the Clerk organisation is unknown to the server.
    await expect(
      w.t
        .withIdentity({ subject: "legacy-admin", ...claims })
        .query(api.machines.listMachines, {}),
    ).rejects.toThrow(/not known to the server/);

    // The operator runs the migration again: it links the default organisation.
    await migrate(w);

    const admin = w.t.withIdentity({ subject: "legacy-admin", ...claims });
    expect((await whoAmI(admin))?.role).toBe("admin");
    const machines = await admin.query(api.machines.listMachines, {});
    expect(machines.map((m) => m._id)).toEqual([w.machine]);
  });

  it("creates a sign-up without organisation claims with no organisation", async () => {
    const w = await seedLegacyWorld(modules);
    await migrate(w);
    configureAnheartOrganization(ANHEART_CLERK_ORG);

    const userId = await legacy(w, "fresh-signup").mutation(
      api.users.getOrCreateUser,
      {},
    );

    const { user, memberships } = await w.t.run(async (ctx) => ({
      user: await ctx.db.get(userId),
      memberships: await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", userId))
        .collect(),
    }));
    expect(user?.organizationId).toBeUndefined();
    expect(memberships).toEqual([]);
  });
});
