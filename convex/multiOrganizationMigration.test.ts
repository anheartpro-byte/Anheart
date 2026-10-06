/// <reference types="vite/client" />
/**
 * ANH-114 EX-4: the multi-organisation migration, in memory. It is never run
 * against a deployment from here.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { attachExistingRowsToAnheart } from "./migrations/multiOrganization";
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
  w.t.mutation(internal.migrations.multiOrganization.attachExistingRowsToAnheart, { batchSize });

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
        config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
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
        config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
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
  it("is an internal mutation, never callable from the site", () => {
    const registration = attachExistingRowsToAnheart as unknown as {
      isInternal?: boolean;
      isPublic?: boolean;
      isMutation?: boolean;
    };
    expect(registration.isMutation).toBe(true);
    expect(registration.isInternal).toBe(true);
    expect(registration.isPublic).not.toBe(true);
  });
});
