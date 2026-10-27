/// <reference types="vite/client" />
/**
 * ANH-159: the figures of the dashboard's first page (`dashboard.counters`).
 *
 * The authorization matrix (`authorization.matrix.ts`) checks what every role
 * of both centres is counted. These tests check the cases the matrix does not
 * express: what a figure leaves out (a removed member, a deleted machine, a
 * session of another organisation), what the Anheart admin alone is counted
 * (rows without an organisation, a restored machine), how an account member of
 * two organisations is counted, what a gestionnaire's figures are made of, and
 * that a machine is never counted online by the server on the strength of a
 * record the site would show offline.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { isFresh, LIVE_FRESH_MS } from "../lib/training";
import { addSession, as, modules, NOW, seedWorld, type World } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

async function addMember(
  w: World,
  name: string,
  organizationId: Id<"organizations">,
  role: "admin" | "gestionnaire" | "user",
  active = true,
): Promise<Id<"users">> {
  return await w.t.run(async (ctx) => {
    const userId = await ctx.db.insert("users", {
      clerkId: name,
      role,
      organizationId,
      firstName: name,
      lastName: "Synthetic",
      email: `${name}@example.invalid`,
      language: "en",
      createdAt: NOW,
    });
    await ctx.db.insert("memberships", { userId, organizationId, role, active });
    return userId;
  });
}

async function addMachine(
  w: World,
  fields: {
    organizationId?: Id<"organizations">;
    status: "online" | "offline" | "in_session";
    lastHeartbeat: number;
    isDeleted?: boolean;
  },
): Promise<Id<"machines">> {
  return await w.t.run((ctx) =>
    ctx.db.insert("machines", {
      name: "Counted machine",
      apiKey: "synthetic-hash",
      createdAt: NOW,
      ...fields,
    }),
  );
}

describe("dashboard.counters: what no figure counts", () => {
  it("counts neither a removed member nor a deleted machine nor a closed session", async () => {
    const w = await seedWorld(modules);
    await addMember(w, "removed", w.orgA, "user", false);
    await addMachine(w, {
      organizationId: w.orgA,
      status: "offline",
      lastHeartbeat: NOW,
      isDeleted: true,
    });
    for (const status of ["completed", "failed"] as const) {
      await addSession(w, { machineId: w.machine, userId: w.patient, status });
    }

    const counted = await as(w.t, "orgAdmin").query(api.dashboard.counters, {});

    expect(counted.members).toEqual({ total: 6, admin: 1, gestionnaire: 2, user: 3 });
    expect(counted.machines?.total).toBe(2);
    expect(counted.sessions).toEqual({ active: 0, pending: 0 });
  });

  it.each(["admin", "orgAdmin", "manager", "patient"] as const)(
    "does not count for %s, as active, a session interrupted for lack of news (ANH-216)",
    async (actor) => {
      const w = await seedWorld(modules);
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "interrupted",
        kind: "auto",
      });
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });

      const counted = await as(w.t, actor).query(api.dashboard.counters, {});

      expect(counted.sessions).toEqual({ active: 1, pending: 0 });
    },
  );

  it("counts nothing of centre B for centre A, however much centre B holds", async () => {
    const w = await seedWorld(modules);
    await addMember(w, "orgBSecond", w.orgB, "user");
    await addMachine(w, { organizationId: w.orgB, status: "online", lastHeartbeat: NOW });
    await addSession(w, { machineId: w.orgBMachine, userId: w.orgBPatient, status: "active" });
    await addSession(w, { machineId: w.orgBMachine, userId: w.orgBPatient, status: "pending" });

    const centreA = await as(w.t, "orgAdmin").query(api.dashboard.counters, {});
    const centreB = await as(w.t, "orgBAdmin").query(api.dashboard.counters, {});

    expect(centreA.members).toEqual({ total: 6, admin: 1, gestionnaire: 2, user: 3 });
    expect(centreA.machines?.total).toBe(2);
    expect(centreA.sessions).toEqual({ active: 0, pending: 0 });
    expect(centreB.members).toEqual({ total: 4, admin: 1, gestionnaire: 1, user: 2 });
    expect(centreB.machines?.total).toBe(2);
    expect(centreB.sessions).toEqual({ active: 1, pending: 1 });
  });
});

/** A further membership of an account seeded in one organisation. */
async function addMembership(
  w: World,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
  role: "admin" | "gestionnaire" | "user",
  active = true,
): Promise<void> {
  await w.t.run(async (ctx) => {
    await ctx.db.insert("memberships", { userId, organizationId, role, active });
  });
}

/** The seeded world as the Anheart admin counts its members: one account each. */
const EVERY_MEMBER = { total: 10, admin: 3, gestionnaire: 3, user: 4 };

describe("dashboard.counters: members are accounts", () => {
  it("counts once, for the Anheart admin, a patient of two organisations, still counted by centre B", async () => {
    const w = await seedWorld(modules);
    const before = await as(w.t, "admin").query(api.dashboard.counters, {});
    await addMembership(w, w.patient, w.orgB, "user");

    const admin = await as(w.t, "admin").query(api.dashboard.counters, {});
    const centreB = await as(w.t, "orgBAdmin").query(api.dashboard.counters, {});

    expect(before.members).toEqual(EVERY_MEMBER);
    expect(admin.members).toEqual(EVERY_MEMBER);
    expect(centreB.members).toEqual({ total: 4, admin: 1, gestionnaire: 1, user: 2 });
  });

  it("counts once in all, and once in each role, a gestionnaire of centre A who is a patient of centre B", async () => {
    const w = await seedWorld(modules);
    await addMembership(w, w.manager, w.orgB, "user");

    const { members } = await as(w.t, "admin").query(api.dashboard.counters, {});

    expect(members).toEqual({ total: 10, admin: 3, gestionnaire: 3, user: 5 });
    // The roles add up to more than the accounts.
    expect(members!.admin + members!.gestionnaire + members!.user).toBe(11);
  });

  it("leaves out a second membership that is no longer active", async () => {
    const w = await seedWorld(modules);
    await addMembership(w, w.patient, w.orgB, "gestionnaire", false);

    const admin = await as(w.t, "admin").query(api.dashboard.counters, {});
    const centreB = await as(w.t, "orgBAdmin").query(api.dashboard.counters, {});

    expect(admin.members).toEqual(EVERY_MEMBER);
    expect(centreB.members).toEqual({ total: 3, admin: 1, gestionnaire: 1, user: 1 });
  });

  it.each([
    ["orgAdmin", { total: 6, admin: 1, gestionnaire: 2, user: 3 }],
    ["orgBAdmin", { total: 4, admin: 1, gestionnaire: 1, user: 2 }],
  ] as const)(
    "counts for %s members whose roles add up to the total, an account of two organisations included",
    async (actor, expected) => {
      const w = await seedWorld(modules);
      await addMembership(w, w.manager, w.orgB, "user");

      const { members } = await as(w.t, actor).query(api.dashboard.counters, {});

      expect(members).toEqual(expected);
      expect(members!.admin + members!.gestionnaire + members!.user).toBe(members!.total);
    },
  );
});

describe("dashboard.counters: the Anheart admin", () => {
  it("counts a restored machine and the rows written before organisations, no one else does", async () => {
    const w = await seedWorld(modules);
    await addMachine(w, {
      organizationId: w.orgA,
      status: "online",
      lastHeartbeat: NOW,
      isDeleted: false,
    });
    const legacy = await addMachine(w, { status: "in_session", lastHeartbeat: NOW });
    await addSession(w, { machineId: legacy, status: "active" });

    const admin = await as(w.t, "admin").query(api.dashboard.counters, {});
    const centreA = await as(w.t, "orgAdmin").query(api.dashboard.counters, {});

    expect(admin.machines?.total).toBe(5);
    expect(admin.sessions).toEqual({ active: 1, pending: 0 });
    expect(centreA.machines?.total).toBe(3);
    expect(centreA.sessions).toEqual({ active: 0, pending: 0 });
  });
});

describe("dashboard.counters: a gestionnaire", () => {
  it("counts their patients who are still patients of their organisation, and no one else", async () => {
    const w = await seedWorld(modules);
    const left = await addMember(w, "left", w.orgA, "user", false);
    const colleague = await addMember(w, "colleague", w.orgA, "gestionnaire");
    await w.t.run(async (ctx) => {
      for (const [userId, organizationId] of [
        [left, w.orgA],
        [colleague, w.orgA],
        // A link written in another organisation is not one of theirs here.
        [w.orgBPatient, w.orgB],
        // Linked twice: one patient.
        [w.patient, w.orgA],
      ] as const) {
        await ctx.db.insert("user_gestionnaires", {
          organizationId,
          userId,
          gestionnaireId: w.manager,
          createdAt: NOW,
          createdBy: w.admin,
        });
      }
    });

    const counted = await as(w.t, "manager").query(api.dashboard.counters, {});

    expect(counted.patients).toBe(1);
    expect(counted.members).toBeNull();
  });

  it("counts the sessions they can open: on their machines, or ridden by themselves", async () => {
    const w = await seedWorld(modules);
    await addSession(w, { machineId: w.machine, userId: w.patient, status: "active" });
    // On a colleague's machine, ridden by the gestionnaire: theirs to open.
    await addSession(w, { machineId: w.otherMachine, userId: w.manager, status: "pending" });
    // On a colleague's machine, ridden by someone else: not theirs.
    await addSession(w, { machineId: w.otherMachine, userId: w.otherPatient, status: "active" });
    // Started at the machine without a rider, on their machine: theirs.
    await addSession(w, { machineId: w.machine, status: "pending" });

    const counted = await as(w.t, "manager").query(api.dashboard.counters, {});

    expect(counted.sessions).toEqual({ active: 1, pending: 2 });
  });

  it("counts the machines they manage in their organisation, deleted ones left out", async () => {
    const w = await seedWorld(modules);
    const deleted = await addMachine(w, {
      organizationId: w.orgA,
      status: "offline",
      lastHeartbeat: NOW,
      isDeleted: true,
    });
    await w.t.run(async (ctx) => {
      for (const [machineId, organizationId] of [
        [deleted, w.orgA],
        // A machine of centre B linked to a gestionnaire of centre A.
        [w.orgBMachine, w.orgB],
      ] as const) {
        await ctx.db.insert("machine_gestionnaires", {
          organizationId,
          machineId,
          gestionnaireId: w.manager,
          isOwner: false,
          createdAt: NOW,
          createdBy: w.admin,
        });
      }
    });

    const counted = await as(w.t, "manager").query(api.dashboard.counters, {});

    expect(counted.machines).toEqual({
      total: 1,
      signals: [{ status: "online", lastHeartbeat: NOW }],
    });
  });
});

describe("dashboard.counters: machines online on the site's rule", () => {
  it("hands the site the last signal of every machine its record calls connected, and the server's clock", async () => {
    const w = await seedWorld(modules);
    const silentFor = LIVE_FRESH_MS + 1000;
    // The server's job has not written "offline" yet: the record still says
    // online, 91 s after the last signal.
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, { lastHeartbeat: NOW - silentFor });
      await ctx.db.patch(w.otherMachine, { status: "in_session" });
    });
    await addMachine(w, { organizationId: w.orgA, status: "offline", lastHeartbeat: NOW });
    vi.setSystemTime(NOW + 2000);

    const counted = await as(w.t, "orgAdmin").query(api.dashboard.counters, {});

    expect(counted.serverNow).toBe(NOW + 2000);
    expect(counted.machines?.total).toBe(3);
    const signals = [...(counted.machines?.signals ?? [])].sort((a, b) =>
      a.status.localeCompare(b.status),
    );
    expect(signals).toEqual([
      { status: "in_session", lastHeartbeat: NOW },
      { status: "online", lastHeartbeat: NOW - silentFor },
    ]);
    // What the site then shows: the silent machine is not fresh, so offline.
    expect(
      signals.map((s) => isFresh(s.lastHeartbeat, counted.serverNow)),
    ).toEqual([true, false]);
  });

  it("gives a patient no machine at all", async () => {
    const w = await seedWorld(modules);
    const counted = await as(w.t, "patient").query(api.dashboard.counters, {});
    expect(counted.machines).toBeNull();
    expect(counted.members).toBeNull();
    expect(counted.patients).toBeNull();
  });
});
