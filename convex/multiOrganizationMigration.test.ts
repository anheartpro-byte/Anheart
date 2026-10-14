/// <reference types="vite/client" />
/**
 * ANH-114 EX-4: the multi-organisation migration, in memory. It is never run
 * against a deployment from here.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import {
  ANHEART_ORG_ID_REQUIRED,
  ANHEART_ORGANIZATION_DELETED_IN_CLERK,
  ANHEART_ORGANIZATION_DUPLICATED,
  attachExistingRowsToAnheart,
  duplicateHoldsData,
  repairDuplicatedAnheartOrganization,
  repairNotConfirmed,
} from "./migrations/multiOrganization";
import {
  configureAnheartOrganization,
  modules,
  NOW,
  seedLegacyWorld,
  type LegacyWorld,
} from "./test.setup";

const ORGANIZATION_TABLES = [
  "machines",
  "users",
  "sessions",
  "training_telemetry",
  "machine_profiles",
  "machine_user_permissions",
  "machine_gestionnaires",
  "user_gestionnaires",
] as const;

/** One machine, three accounts, one session, five telemetry points, four links. */
const LEGACY_ROWS = 14;

const migrate = (w: LegacyWorld, batchSize?: number) =>
  w.t.mutation(
    internal.migrations.multiOrganization.attachExistingRowsToAnheart,
    { batchSize },
  );

/**
 * The organisation of every row of every organisation-scoped table; null for a
 * row that has none.
 */
async function organizationsByTable(w: LegacyWorld) {
  return await w.t.run(async (ctx) => {
    const result: Record<string, Array<Id<"organizations"> | null>> = {};
    for (const table of ORGANIZATION_TABLES) {
      const rows = await ctx.db.query(table).collect();
      result[table] = rows.map((row) => row.organizationId ?? null);
    }
    return result;
  });
}

const organizations = (w: LegacyWorld) =>
  w.t.run((ctx) => ctx.db.query("organizations").collect());
const memberships = (w: LegacyWorld) =>
  w.t.run((ctx) => ctx.db.query("memberships").collect());

afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

describe("EX-4 migration: attach existing rows to the Anheart organisation", () => {
  it("creates the Anheart organisation and attaches every existing row to it", async () => {
    const w = await seedLegacyWorld(modules);

    const result = await migrate(w);

    const all = await organizations(w);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({
      name: "Anheart",
      slug: "anheart",
      settings: { requirePrescription: false, language: "fr" },
    });
    expect(all[0].clerkOrgId).toBeUndefined();
    expect(result).toEqual({
      organizationId: all[0]._id,
      attached: LEGACY_ROWS,
      done: true,
    });
    const byTable = await organizationsByTable(w);
    for (const table of ORGANIZATION_TABLES) {
      expect(byTable[table].length, table).toBeGreaterThan(0);
      expect(
        byTable[table].every((id) => id === all[0]._id),
        table,
      ).toBe(true);
    }
  });

  it("mirrors the role of each account in an active membership", async () => {
    const w = await seedLegacyWorld(modules);

    const { organizationId } = await migrate(w);

    const rows = await memberships(w);
    expect(
      rows
        .map((m) => ({ userId: m.userId, role: m.role, active: m.active }))
        .sort((a, b) => a.role.localeCompare(b.role)),
    ).toEqual([
      { userId: w.admin, role: "admin", active: true },
      { userId: w.manager, role: "gestionnaire", active: true },
      { userId: w.patient, role: "user", active: true },
    ]);
    expect(rows.every((m) => m.organizationId === organizationId)).toBe(true);
  });

  it("changes nothing when it runs again", async () => {
    const w = await seedLegacyWorld(modules);
    const first = await migrate(w);
    const before = await organizationsByTable(w);

    const second = await migrate(w);

    expect(second).toEqual({
      organizationId: first.organizationId,
      attached: 0,
      done: true,
    });
    expect(await organizations(w)).toHaveLength(1);
    expect(await memberships(w)).toHaveLength(3);
    expect(await organizationsByTable(w)).toEqual(before);
  });

  it("works in bounded batches and continues by itself until nothing is left", async () => {
    vi.useFakeTimers();
    const w = await seedLegacyWorld(modules);

    const first = await migrate(w, 4);

    expect(first).toMatchObject({ attached: 4, done: false });
    const partial = await organizationsByTable(w);
    expect(
      Object.values(partial)
        .flat()
        .filter((id) => id === null),
    ).toHaveLength(LEGACY_ROWS - 4);

    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    const complete = await organizationsByTable(w);
    expect(
      Object.values(complete)
        .flat()
        .every((id) => id === first.organizationId),
    ).toBe(true);
    expect(await organizations(w)).toHaveLength(1);
    expect(await memberships(w)).toHaveLength(3);
  });

  it("still makes progress with a batch size below one", async () => {
    vi.useFakeTimers();
    const w = await seedLegacyWorld(modules);

    const first = await migrate(w, 0);
    expect(first).toMatchObject({ attached: 1, done: false });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    const complete = await organizationsByTable(w);
    expect(
      Object.values(complete)
        .flat()
        .every((id) => id === first.organizationId),
    ).toBe(true);
  });

  it("never moves a row that already has an organisation, and what a machine wrote follows its machine", async () => {
    const w = await seedLegacyWorld(modules);
    // A second organisation that already owns a machine and an account, with
    // rows written for them that carry no organisation yet.
    const other = await w.t.run(async (ctx) => {
      const organization = await ctx.db.insert("organizations", {
        clerkOrgId: "org_synthetic_other",
        name: "Other centre",
        slug: "other-centre",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      });
      const machine = await ctx.db.insert("machines", {
        organizationId: organization,
        name: "Other machine",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW,
        createdAt: NOW,
      });
      const member = await ctx.db.insert("users", {
        clerkId: "other-patient",
        role: "user" as const,
        organizationId: organization,
        firstName: "Other",
        lastName: "Patient",
        email: "other-patient@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      });
      const session = await ctx.db.insert("sessions", {
        machineId: machine,
        status: "completed" as const,
        startedAt: NOW,
        channels: ["ECG"],
      });
      const right = await ctx.db.insert("machine_user_permissions", {
        machineId: machine,
        userId: member,
        grantedBy: w.admin,
        createdAt: NOW,
      });
      const link = await ctx.db.insert("user_gestionnaires", {
        userId: member,
        gestionnaireId: w.manager,
        createdAt: NOW,
        createdBy: w.admin,
      });
      return { organization, machine, member, session, right, link };
    });

    const { organizationId: anheart } = await migrate(w);

    const rows = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(other.machine),
      member: await ctx.db.get(other.member),
      session: await ctx.db.get(other.session),
      right: await ctx.db.get(other.right),
      link: await ctx.db.get(other.link),
      legacySession: await ctx.db.get(w.session),
      memberMemberships: await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", other.member))
        .collect(),
    }));
    expect(anheart).not.toBe(other.organization);
    // Already attached: untouched, and no membership invented in Anheart.
    expect(rows.machine?.organizationId).toBe(other.organization);
    expect(rows.member?.organizationId).toBe(other.organization);
    expect(rows.memberMemberships).toEqual([]);
    // Written by the other machine, or about the other organisation's patient.
    expect(rows.session?.organizationId).toBe(other.organization);
    expect(rows.right?.organizationId).toBe(other.organization);
    expect(rows.link?.organizationId).toBe(other.organization);
    // The legacy rows went to Anheart.
    expect(rows.legacySession?.organizationId).toBe(anheart);
  });

  it("attaches to Anheart a row whose machine or patient no longer exists", async () => {
    const w = await seedLegacyWorld(modules);
    const orphans = await w.t.run(async (ctx) => {
      const gone = await ctx.db.insert("machines", {
        name: "Removed machine",
        apiKey: "synthetic-hash",
        status: "offline" as const,
        lastHeartbeat: 0,
        createdAt: NOW,
      });
      const goneUser = await ctx.db.insert("users", {
        clerkId: "removed-patient",
        role: "user" as const,
        firstName: "Removed",
        lastName: "Patient",
        email: "removed-patient@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      });
      const session = await ctx.db.insert("sessions", {
        machineId: gone,
        status: "completed" as const,
        startedAt: NOW,
        channels: ["ECG"],
      });
      const link = await ctx.db.insert("user_gestionnaires", {
        userId: goneUser,
        gestionnaireId: w.manager,
        createdAt: NOW,
        createdBy: w.admin,
      });
      await ctx.db.delete(gone);
      await ctx.db.delete(goneUser);
      return { session, link };
    });

    const { organizationId } = await migrate(w);

    const rows = await w.t.run(async (ctx) => ({
      session: await ctx.db.get(orphans.session),
      link: await ctx.db.get(orphans.link),
    }));
    expect(rows.session?.organizationId).toBe(organizationId);
    expect(rows.link?.organizationId).toBe(organizationId);
  });

  it("keeps the membership an account already has in Anheart", async () => {
    const w = await seedLegacyWorld(modules);
    const { organizationId } = await migrate(w);
    // An account that lost its main organisation but kept its membership.
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.manager, { organizationId: undefined });
    });

    const again = await migrate(w);

    expect(again).toMatchObject({ attached: 1, done: true });
    const rows = await memberships(w);
    expect(rows.filter((m) => m.userId === w.manager)).toHaveLength(1);
    const manager = await w.t.run((ctx) => ctx.db.get(w.manager));
    expect(manager?.organizationId).toBe(organizationId);
  });
});

describe("EX-4 migration: link with the Clerk organisation named by ANHEART_ORG_ID", () => {
  it("links the existing default organisation instead of creating another one", async () => {
    const w = await seedLegacyWorld(modules);
    const first = await migrate(w);

    configureAnheartOrganization("org_synthetic_anheart");
    const second = await migrate(w);

    const all = await organizations(w);
    expect(all).toHaveLength(1);
    expect(all[0]._id).toBe(first.organizationId);
    expect(all[0].clerkOrgId).toBe("org_synthetic_anheart");
    expect(second).toEqual({
      organizationId: first.organizationId,
      attached: 0,
      done: true,
    });
    // Once linked, a further run finds it by its Clerk identifier.
    expect((await migrate(w)).organizationId).toBe(first.organizationId);
    expect(await organizations(w)).toHaveLength(1);
  });

  it("creates the organisation already linked when the variable is set first", async () => {
    const w = await seedLegacyWorld(modules);
    configureAnheartOrganization("org_synthetic_anheart");

    const result = await migrate(w);

    const all = await organizations(w);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({
      _id: result.organizationId,
      clerkOrgId: "org_synthetic_anheart",
      slug: "anheart",
    });
  });

  it("uses the mirror of that Clerk organisation when it already exists", async () => {
    const w = await seedLegacyWorld(modules);
    configureAnheartOrganization("org_synthetic_anheart");
    const mirrored = await w.t.run((ctx) =>
      ctx.db.insert("organizations", {
        clerkOrgId: "org_synthetic_anheart",
        name: "Anheart SAS",
        slug: "anheart-sas",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      }),
    );

    const result = await migrate(w);

    expect(result.organizationId).toBe(mirrored);
    expect(await organizations(w)).toHaveLength(1);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.organizationId).toBe(mirrored);
  });

  // ANH-187 review: the Clerk webhook received the Anheart organisation before
  // ANHEART_ORG_ID was set, so it created its own row. Linking that empty row would
  // leave every existing machine, session and account outside Anheart.
  it("refuses, and changes nothing, when the webhook mirrored the Clerk organisation before ANHEART_ORG_ID was set", async () => {
    const w = await seedLegacyWorld(modules);
    const { organizationId: defaultOrganization } = await migrate(w);
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "evt_anheart_created",
      event: {
        kind: "organization",
        deleted: false,
        organization: {
          id: "org_synthetic_anheart",
          name: "Anheart",
          slug: "anheart",
          createdAt: NOW,
          updatedAt: NOW,
        },
        occurredAt: NOW,
        updatedAt: NOW,
      },
    });
    const before = await organizations(w);
    configureAnheartOrganization("org_synthetic_anheart");

    await expect(migrate(w)).rejects.toThrow(ANHEART_ORGANIZATION_DUPLICATED);

    expect(await organizations(w)).toEqual(before);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.organizationId).toBe(defaultOrganization);
  });

  it("does not take a client organisation whose slug is the default one", async () => {
    const w = await seedLegacyWorld(modules);
    const client = await w.t.run((ctx) =>
      ctx.db.insert("organizations", {
        clerkOrgId: "org_synthetic_client",
        name: "A client",
        slug: "anheart",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      }),
    );

    const result = await migrate(w);

    expect(result.organizationId).not.toBe(client);
    expect(await organizations(w)).toHaveLength(2);
  });
});

describe("EX-4 migration: who can run it", () => {
  it.each([
    ["the migration", attachExistingRowsToAnheart],
    [
      "the repair of a duplicated Anheart organisation",
      repairDuplicatedAnheartOrganization,
    ],
  ])(
    "%s is an internal mutation, never callable from the site",
    (_name, fn) => {
      const registration = fn as unknown as {
        isInternal?: boolean;
        isPublic?: boolean;
        isMutation?: boolean;
      };
      expect(registration.isMutation).toBe(true);
      expect(registration.isInternal).toBe(true);
      expect(registration.isPublic).not.toBe(true);
    },
  );
});

// ANH-225 EX-1: the way out of the state the migration refuses above.
describe("ANH-225 EX-1 repair: two Anheart organisations", () => {
  const ANHEART_CLERK_ORG = "org_synthetic_anheart";
  /** The Clerk Anheart organisation as the webhook received it. */
  const SNAPSHOT = {
    id: ANHEART_CLERK_ORG,
    name: "Anheart SAS",
    slug: "anheart-sas",
    createdAt: NOW,
    updatedAt: NOW + 5,
  };

  /** A dry run unless `apply` is true. */
  const repair = (
    w: LegacyWorld,
    args: { apply?: boolean; expectedName?: string } = {},
  ) =>
    w.t.mutation(
      internal.migrations.multiOrganization.repairDuplicatedAnheartOrganization,
      args,
    );
  /** The applying run, confirmed with the name the dry run reports. */
  const CONFIRMED = { apply: true, expectedName: SNAPSHOT.name };

  const joined = (
    w: LegacyWorld,
    eventId: string,
    member: {
      userId: string;
      role: "admin" | "gestionnaire" | "user";
      updatedAt?: number;
    },
  ) =>
    w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId,
      event: {
        kind: "membership",
        deleted: false,
        organization: SNAPSHOT,
        occurredAt: member.updatedAt ?? NOW + 10,
        updatedAt: member.updatedAt ?? NOW + 10,
        membershipId: `orgmem_${member.userId}`,
        userId: member.userId,
        firstName: member.userId,
        lastName: "Clerk",
        email: `${member.userId}@example.invalid`,
        verifiedEmails: [`${member.userId}@example.invalid`],
        role: member.role,
      },
    });

  /**
   * The migration ran, then the webhook received the Clerk Anheart
   * organisation and three memberships before ANHEART_ORG_ID was set: the
   * legacy admin and the legacy gestionnaire (whom Clerk makes an admin), and
   * an account Convex did not know, which the webhook created. Then the
   * variable was set.
   */
  async function duplicatedWorld() {
    const w = await seedLegacyWorld(modules);
    const { organizationId: defaultOrganization } = await migrate(w);
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "evt_anheart_created",
      event: {
        kind: "organization",
        deleted: false,
        organization: SNAPSHOT,
        occurredAt: NOW + 5,
        updatedAt: NOW + 5,
      },
    });
    await joined(w, "evt_admin", { userId: "legacy-admin", role: "admin" });
    await joined(w, "evt_manager", { userId: "legacy-manager", role: "admin" });
    await joined(w, "evt_new", { userId: "user_new", role: "gestionnaire" });
    const duplicate = (await organizations(w)).find(
      (row) => row.clerkOrgId === ANHEART_CLERK_ORG,
    );
    if (!duplicate) throw new Error("The webhook did not create its row");
    configureAnheartOrganization(ANHEART_CLERK_ORG);
    return { w, defaultOrganization, duplicate: duplicate._id };
  }

  const userByClerkId = (w: LegacyWorld, clerkId: string) =>
    w.t.run((ctx) =>
      ctx.db
        .query("users")
        .withIndex("by_clerk_id", (q) => q.eq("clerkId", clerkId))
        .unique(),
    );

  /** Every row the repair may write, to prove a refusal wrote none. */
  const mirror = (w: LegacyWorld) =>
    w.t.run(async (ctx) => ({
      organizations: await ctx.db.query("organizations").collect(),
      memberships: await ctx.db.query("memberships").collect(),
      users: await ctx.db.query("users").collect(),
    }));

  it("by default only describes what it would do, and writes nothing", async () => {
    const { w, defaultOrganization, duplicate } = await duplicatedWorld();
    const before = await mirror(w);

    const described = await repair(w);

    expect(described).toEqual({
      status: "dry_run",
      absorbs: {
        organizationId: duplicate,
        name: "Anheart SAS",
        slug: "anheart-sas",
        clerkOrgId: ANHEART_CLERK_ORG,
        memberships: 3,
        admins: 2,
      },
      links: {
        organizationId: defaultOrganization,
        name: "Anheart",
        slug: "anheart",
      },
      movedMemberships: 1,
      mergedMemberships: 2,
      movedUsers: 1,
    });
    // A name without `apply` is still a dry run.
    expect(await repair(w, { expectedName: SNAPSHOT.name })).toMatchObject({
      status: "dry_run",
    });
    expect(await mirror(w)).toEqual(before);
    await expect(migrate(w)).rejects.toThrow(ANHEART_ORGANIZATION_DUPLICATED);
  });

  it.each([
    ["without a name", { apply: true }],
    ["with another name", { apply: true, expectedName: "Anheart" }],
    ["with the slug for a name", { apply: true, expectedName: "anheart-sas" }],
  ])("refuses to apply, and writes nothing, %s", async (_case, args) => {
    const { w } = await duplicatedWorld();
    const before = await mirror(w);

    await expect(repair(w, args)).rejects.toThrow(
      repairNotConfirmed("Anheart SAS"),
    );
    expect(await mirror(w)).toEqual(before);
  });

  it("folds the webhook's row into the default organisation and links it to Clerk, once confirmed", async () => {
    const { w, defaultOrganization } = await duplicatedWorld();
    await expect(migrate(w)).rejects.toThrow(ANHEART_ORGANIZATION_DUPLICATED);

    const result = await repair(w, CONFIRMED);

    expect(result).toEqual({
      status: "repaired",
      organizationId: defaultOrganization,
      movedMemberships: 1,
      mergedMemberships: 2,
      movedUsers: 1,
    });
    // One organisation left: the default one, now the mirror of the Clerk
    // organisation, with what the webhook had received for it.
    const all = await organizations(w);
    expect(all).toHaveLength(1);
    expect(all[0]).toMatchObject({
      _id: defaultOrganization,
      clerkOrgId: ANHEART_CLERK_ORG,
      name: "Anheart SAS",
      slug: "anheart-sas",
      clerkUpdatedAt: NOW + 5,
      clerkEventAt: NOW + 5,
    });
    // One membership per account, all in the default organisation. Clerk's
    // role wins for a member the migration had already enrolled.
    const newcomer = await userByClerkId(w, "user_new");
    const rows = (await memberships(w)).map((m) => ({
      userId: m.userId,
      organizationId: m.organizationId,
      role: m.role,
      active: m.active,
      clerkMembershipId: m.clerkMembershipId,
    }));
    expect(rows).toHaveLength(4);
    expect(rows).toEqual(
      expect.arrayContaining([
        {
          userId: w.admin,
          organizationId: defaultOrganization,
          role: "admin",
          active: true,
          clerkMembershipId: "orgmem_legacy-admin",
        },
        {
          userId: w.manager,
          organizationId: defaultOrganization,
          role: "admin",
          active: true,
          clerkMembershipId: "orgmem_legacy-manager",
        },
        {
          userId: w.patient,
          organizationId: defaultOrganization,
          role: "user",
          active: true,
          clerkMembershipId: undefined,
        },
        {
          userId: newcomer?._id,
          organizationId: defaultOrganization,
          role: "gestionnaire",
          active: true,
          clerkMembershipId: "orgmem_user_new",
        },
      ]),
    );
    // The account the webhook created now has the default organisation.
    expect(newcomer?.organizationId).toBe(defaultOrganization);
    // The existing rows never moved, and the migration now runs.
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.organizationId).toBe(defaultOrganization);
    expect(await migrate(w)).toEqual({
      organizationId: defaultOrganization,
      attached: 0,
      done: true,
    });
  });

  it("leaves the webhook writing to the default organisation, newer events only", async () => {
    const { w, defaultOrganization } = await duplicatedWorld();
    await repair(w, CONFIRMED);

    // A delivery older than what the repair carried over changes nothing.
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "evt_old_rename",
      event: {
        kind: "organization",
        deleted: false,
        organization: { ...SNAPSHOT, name: "Old name", updatedAt: NOW + 1 },
        occurredAt: NOW + 1,
        updatedAt: NOW + 1,
      },
    });
    // A later role change lands on the membership the repair kept.
    await joined(w, "evt_new_promoted", {
      userId: "user_new",
      role: "admin",
      updatedAt: NOW + 20,
    });

    const all = await organizations(w);
    expect(all).toHaveLength(1);
    expect(all[0].name).toBe("Anheart SAS");
    const newcomer = await userByClerkId(w, "user_new");
    const theirs = (await memberships(w)).filter(
      (m) => m.userId === newcomer?._id,
    );
    expect(theirs).toEqual([
      expect.objectContaining({
        organizationId: defaultOrganization,
        role: "admin",
        active: true,
      }),
    ]);
  });

  it("finds nothing to do once repaired", async () => {
    const { w } = await duplicatedWorld();
    await repair(w, CONFIRMED);
    const before = await mirror(w);

    expect(await repair(w)).toEqual({ status: "nothing_to_repair" });
    expect(await repair(w, CONFIRMED)).toEqual({
      status: "nothing_to_repair",
    });
    expect(await mirror(w)).toEqual(before);
  });

  it("finds nothing to do when the procedure was followed in order", async () => {
    const w = await seedLegacyWorld(modules);
    configureAnheartOrganization(ANHEART_CLERK_ORG);
    await migrate(w);
    const before = await mirror(w);

    expect(await repair(w)).toEqual({ status: "nothing_to_repair" });
    expect(await mirror(w)).toEqual(before);
  });

  it("refuses, and changes nothing, while ANHEART_ORG_ID is unset", async () => {
    const { w } = await duplicatedWorld();
    configureAnheartOrganization(null);
    const before = await mirror(w);

    for (const args of [{}, CONFIRMED]) {
      await expect(repair(w, args)).rejects.toThrow(ANHEART_ORG_ID_REQUIRED);
    }
    expect(await mirror(w)).toEqual(before);
  });

  it("refuses, and changes nothing, when Clerk deleted that organisation", async () => {
    const { w, duplicate } = await duplicatedWorld();
    await w.t.run((ctx) => ctx.db.patch(duplicate, { deletedAt: NOW + 30 }));
    const before = await mirror(w);

    for (const args of [{}, CONFIRMED]) {
      await expect(repair(w, args)).rejects.toThrow(
        ANHEART_ORGANIZATION_DELETED_IN_CLERK,
      );
    }
    expect(await mirror(w)).toEqual(before);
  });

  it.each([
    [
      "machines",
      async (w: LegacyWorld, organizationId: Id<"organizations">) => {
        await w.t.run((ctx) =>
          ctx.db.insert("machines", {
            organizationId,
            name: "Machine of the duplicate",
            apiKey: "synthetic-hash",
            status: "offline" as const,
            lastHeartbeat: NOW,
            createdAt: NOW,
          }),
        );
      },
    ],
    [
      "user_gestionnaires",
      async (w: LegacyWorld, organizationId: Id<"organizations">) => {
        await w.t.run((ctx) =>
          ctx.db.insert("user_gestionnaires", {
            organizationId,
            userId: w.patient,
            gestionnaireId: w.manager,
            createdAt: NOW,
            createdBy: w.admin,
          }),
        );
      },
    ],
    [
      "patient_invitations",
      async (w: LegacyWorld, organizationId: Id<"organizations">) => {
        await w.t.run((ctx) =>
          ctx.db.insert("patient_invitations", {
            organizationId,
            email: "invited@example.invalid",
            requestedBy: w.admin,
            gestionnaireIds: [],
            status: "pending" as const,
            createdAt: NOW,
            updatedAt: NOW,
          }),
        );
      },
    ],
  ])(
    "refuses, and changes nothing, when the webhook's row holds data (%s)",
    async (table, write) => {
      const { w, duplicate } = await duplicatedWorld();
      await write(w, duplicate);
      const before = await mirror(w);

      for (const args of [{}, CONFIRMED]) {
        await expect(repair(w, args)).rejects.toThrow(
          duplicateHoldsData(table),
        );
      }
      expect(await mirror(w)).toEqual(before);
    },
  );

  // ANH-225 review: ANHEART_ORG_ID copied from a client organisation the
  // webhook already mirrors. Nothing in the database tells it from Anheart's:
  // the dry run shows its name, and applying needs that very name.
  describe("when ANHEART_ORG_ID names a client organisation by mistake", () => {
    const CLIENT = {
      id: "org_synthetic_client",
      name: "Clinique Exemple",
      slug: "clinique-exemple",
      createdAt: NOW,
      updatedAt: NOW + 5,
    };

    async function wrongVariableWorld() {
      const w = await seedLegacyWorld(modules);
      const { organizationId: defaultOrganization } = await migrate(w);
      // The webhook receives a client organisation and its admin.
      await w.t.mutation(internal.clerkWebhook.applyEvent, {
        eventId: "evt_client_admin",
        event: {
          kind: "membership",
          deleted: false,
          organization: CLIENT,
          occurredAt: NOW + 10,
          updatedAt: NOW + 10,
          membershipId: "orgmem_client_admin",
          userId: "user_client_admin",
          firstName: "Client",
          lastName: "Admin",
          email: "client-admin@example.invalid",
          verifiedEmails: ["client-admin@example.invalid"],
          role: "admin",
        },
      });
      configureAnheartOrganization(CLIENT.id);
      return { w, defaultOrganization };
    }

    it("the dry run shows the client's name, slug and Clerk identifier", async () => {
      const { w } = await wrongVariableWorld();
      await expect(migrate(w)).rejects.toThrow(ANHEART_ORGANIZATION_DUPLICATED);

      expect(await repair(w)).toMatchObject({
        status: "dry_run",
        absorbs: {
          name: "Clinique Exemple",
          slug: "clinique-exemple",
          clerkOrgId: CLIENT.id,
          memberships: 1,
          admins: 1,
        },
      });
    });

    it("an operator who confirms the Anheart organisation is refused, and nothing moves", async () => {
      const { w, defaultOrganization } = await wrongVariableWorld();
      const before = await mirror(w);

      await expect(
        repair(w, { apply: true, expectedName: "Anheart" }),
      ).rejects.toThrow(repairNotConfirmed("Clinique Exemple"));

      expect(await mirror(w)).toEqual(before);
      const defaultRow = await w.t.run((ctx) =>
        ctx.db.get(defaultOrganization),
      );
      expect(defaultRow?.clerkOrgId).toBeUndefined();
    });

    it("only confirming the client's own name applies it: the check is the operator's", async () => {
      const { w, defaultOrganization } = await wrongVariableWorld();

      expect(
        await repair(w, { apply: true, expectedName: "Clinique Exemple" }),
      ).toMatchObject({
        status: "repaired",
        organizationId: defaultOrganization,
      });
    });
  });
});
