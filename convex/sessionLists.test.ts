/// <reference types="vite/client" />
/**
 * ANH-158: every list of sessions applies the caller's rights before it cuts.
 *
 * 300 sessions over the three machines of the world (`machine` and
 * `otherMachine` in centre A, `orgBMachine` in centre B), started in an order
 * that is not the order they were written in, some at the same instant, some
 * ridden from the other centre, some without an organisation. Each actor reads
 * them page by page, 50 at a time, and must get exactly the sessions the rule
 * below lets them read, most recent start first.
 *
 * The rule is written here from docs/convex.md (section 3, `canAccessSession`),
 * not from the code: the Anheart admin reads everything; anyone else reads the
 * sessions of their own organisation only, an organisation admin all of them,
 * a gestionnaire those of the machines they manage and those they ride, a user
 * those they ride.
 */
import { beforeAll, describe, expect, it } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import {
  as,
  modules,
  NOW,
  seedWorld,
  USER_ACTORS,
  type Actor,
  type World,
} from "./test.setup";

type UserActor = (typeof USER_ACTORS)[number];
type MachineName = "machine" | "otherMachine" | "orgBMachine";
type Status = "pending" | "active" | "completed" | "failed";

/** One seeded session, as the rule reads it. */
type Seeded = {
  id: Id<"sessions">;
  rank: number; // order of writing: a later row was written later
  machine: MachineName;
  rider: UserActor | null;
  organization: "A" | "B" | null;
  status: Status;
  startedAt: number;
};

const MACHINES: MachineName[] = ["machine", "otherMachine", "orgBMachine"];
const STATUSES: Status[] = ["completed", "failed", "active", "pending"];
/** Who rides each machine, in turn. `null`: started at the machine, no rider. */
const RIDERS: Record<MachineName, (UserActor | null)[]> = {
  // otherManager rides a machine they do not manage.
  machine: [
    "patient",
    "otherPatient",
    "patient",
    null,
    "stranger",
    "otherManager",
  ],
  // manager rides a machine they do not manage.
  otherMachine: ["otherPatient", "patient", "manager", null],
  // Riders of centre A on a machine of centre B: the session is centre B's.
  orgBMachine: ["orgBPatient", null, "patient", "orgBManager", "manager"],
};

const PAGE = 50;

/** What each actor may read, from docs/convex.md section 3. */
const MAY_READ: Record<UserActor, (s: Seeded) => boolean> = {
  admin: () => true,
  orgAdmin: (s) => s.organization === "A",
  orgBAdmin: (s) => s.organization === "B",
  manager: (s) =>
    s.organization === "A" &&
    (s.machine === "machine" || s.rider === "manager"),
  otherManager: (s) =>
    s.organization === "A" &&
    (s.machine === "otherMachine" || s.rider === "otherManager"),
  orgBManager: (s) =>
    s.organization === "B" &&
    (s.machine === "orgBMachine" || s.rider === "orgBManager"),
  patient: (s) => s.organization === "A" && s.rider === "patient",
  otherPatient: (s) => s.organization === "A" && s.rider === "otherPatient",
  stranger: (s) => s.organization === "A" && s.rider === "stranger",
  orgBPatient: (s) => s.organization === "B" && s.rider === "orgBPatient",
};

/** Most recent start first; at the same start, the later written first. */
function newestFirst(a: Seeded, b: Seeded): number {
  return b.startedAt - a.startedAt || b.rank - a.rank;
}

async function seedSessions(w: World): Promise<Seeded[]> {
  const seeded: Seeded[] = [];
  for (let rank = 0; rank < 300; rank++) {
    const machine = MACHINES[rank % 3];
    const turn = Math.floor(rank / 3);
    const rider = RIDERS[machine][turn % RIDERS[machine].length];
    const status = STATUSES[(rank + turn) % 4];
    // A shuffle of 250 instants: the start order is not the writing order,
    // and 50 sessions share their start with another one.
    const startedAt = NOW - ((rank * 7919) % 250) * 60_000;
    // A few rows from before the multi-organisation migration.
    const organization =
      rank % 50 === 7 ? null : machine === "orgBMachine" ? "B" : "A";
    const id = await w.t.run(async (ctx) =>
      ctx.db.insert("sessions", {
        organizationId:
          organization === null
            ? undefined
            : organization === "A"
              ? w.orgA
              : w.orgB,
        machineId: w[machine],
        userId: rider === null ? undefined : w[rider],
        status,
        startedAt,
        channels: ["ECG"],
        kind: "auto",
        origin: rider === null ? "local" : "remote",
      }),
    );
    seeded.push({ id, rank, machine, rider, organization, status, startedAt });
  }
  return seeded;
}

type Filters = {
  status?: Status;
  machineId?: Id<"machines">;
  userId?: Id<"users">;
};

/** Every page `listSessionsPaginated` serves the actor, first to last. */
async function pagesFor(
  w: World,
  actor: Actor,
  filters: Filters,
): Promise<Id<"sessions">[][]> {
  const pages: Id<"sessions">[][] = [];
  let cursor: string | null = null;
  for (;;) {
    const result: FunctionReturnType<
      typeof api.sessions.listSessionsPaginated
    > = await as(w.t, actor).query(api.sessions.listSessionsPaginated, {
      ...filters,
      paginationOpts: { numItems: PAGE, cursor },
    });
    pages.push(result.page.map((row) => row._id));
    if (result.isDone) return pages;
    if (pages.length > 300 / PAGE + 2) throw new Error("Paging never ends");
    cursor = result.continueCursor;
  }
}

/** Fails unless the pages hold `expected`, in order, 50 to a page. */
function expectPages(pages: Id<"sessions">[][], expected: Seeded[]) {
  for (const page of pages.slice(0, -1)) expect(page).toHaveLength(PAGE);
  expect(pages[pages.length - 1].length).toBeLessThanOrEqual(PAGE);
  expect(pages.flat()).toEqual(expected.map((s) => s.id));
}

describe("ANH-158 lists of sessions: 300 sessions, 3 machines, 2 centres", () => {
  let w: World;
  let seeded: Seeded[];
  const readableBy = (actor: UserActor) =>
    seeded.filter(MAY_READ[actor]).sort(newestFirst);

  beforeAll(async () => {
    w = await seedWorld(modules);
    seeded = await seedSessions(w);
  });

  it("is a world where the cut matters", () => {
    // Several pages for most roles, and sessions shared, crossed and orphaned.
    expect(readableBy("admin")).toHaveLength(300);
    expect(readableBy("orgAdmin").length).toBeGreaterThan(3 * PAGE);
    expect(readableBy("orgBAdmin").length).toBeGreaterThan(PAGE);
    expect(readableBy("manager").length).toBeGreaterThan(PAGE);
    expect(readableBy("patient").length).toBeGreaterThan(PAGE);
    expect(readableBy("stranger").length).toBeGreaterThan(0);
    expect(seeded.filter((s) => s.organization === null)).toHaveLength(6);
    expect(new Set(seeded.map((s) => s.startedAt)).size).toBe(250);
    const byStart = [...seeded].sort(newestFirst).map((s) => s.rank);
    expect(byStart).not.toEqual([...byStart].sort((a, b) => b - a));
  });

  it.each(USER_ACTORS)(
    "pages %s through exactly the sessions they may read, most recent start first",
    async (actor) => {
      expectPages(await pagesFor(w, actor, {}), readableBy(actor));
    },
  );

  it.each(
    USER_ACTORS.flatMap((actor) =>
      STATUSES.map((status) => [actor, status] as const),
    ),
  )("pages %s through their %s sessions only", async (actor, status) => {
    expectPages(
      await pagesFor(w, actor, { status }),
      readableBy(actor).filter((s) => s.status === status),
    );
  });

  it.each(
    USER_ACTORS.flatMap((actor) =>
      (["patient", "orgBPatient", "manager"] as const).map(
        (rider) => [actor, rider] as const,
      ),
    ),
  )(
    "pages %s through the sessions ridden by %s that they may read",
    async (actor, rider) => {
      expectPages(
        await pagesFor(w, actor, { userId: w[rider] }),
        readableBy(actor).filter((s) => s.rider === rider),
      );
    },
  );

  it.each(
    USER_ACTORS.flatMap((actor) =>
      MACHINES.map((machine) => [actor, machine] as const),
    ),
  )(
    "pages %s through the completed sessions on %s that they may read",
    async (actor, machine) => {
      expectPages(
        await pagesFor(w, actor, {
          machineId: w[machine],
          status: "completed",
        }),
        readableBy(actor).filter(
          (s) => s.machine === machine && s.status === "completed",
        ),
      );
    },
  );

  it.each(USER_ACTORS)(
    "gives %s the newest of their sessions in a short list",
    async (actor) => {
      const expected = readableBy(actor).map((s) => s.id);
      const reader = as(w.t, actor);
      const ids = (rows: { _id: Id<"sessions"> }[]) => rows.map((r) => r._id);

      expect(
        ids(await reader.query(api.sessions.listSessions, { limit: 10 })),
      ).toEqual(expected.slice(0, 10));
      // 50 when no limit is given.
      expect(ids(await reader.query(api.sessions.listSessions, {}))).toEqual(
        expected.slice(0, 50),
      );
      expect(
        ids(
          await reader.query(api.sessions.listSessions, {
            status: "pending",
            limit: 5,
          }),
        ),
      ).toEqual(
        readableBy(actor)
          .filter((s) => s.status === "pending")
          .slice(0, 5)
          .map((s) => s.id),
      );
    },
  );

  it.each(USER_ACTORS)(
    "gives %s their 100 newest completed sessions for the reports of the previous site",
    async (actor) => {
      const completed = await as(w.t, actor).query(
        api.sessions.getCompletedSessionsForUser,
        {},
      );
      expect(completed.map((r) => r._id)).toEqual(
        readableBy(actor)
          .filter((s) => s.status === "completed")
          .slice(0, 100)
          .map((s) => s.id),
      );
    },
  );

  it("names each rider, or says there is none, with the machine, the status and the origin", async () => {
    const result = await as(w.t, "admin").query(
      api.sessions.listSessionsPaginated,
      { paginationOpts: { numItems: 300, cursor: null } },
    );
    expect(result.page).toHaveLength(300);
    for (const row of result.page) {
      const s = seeded.find((candidate) => candidate.id === row._id)!;
      expect(row).toMatchObject({
        status: s.status,
        startedAt: s.startedAt,
        patientName: s.rider === null ? "Unknown" : `${s.rider} Synthetic`,
        machineName: "Synthetic machine",
        kind: "auto",
        origin: s.rider === null ? "local" : "remote",
      });
    }
  });
});

describe("ANH-158 lists of sessions: what the rule leaves out", () => {
  it("leaves out a session whose machine belongs to another organisation", async () => {
    // A row that contradicts itself: centre A's, on centre B's machine.
    const w = await seedWorld(modules);
    const [mixed, own] = await w.t.run(async (ctx) => {
      const row = {
        machineId: w.orgBMachine,
        status: "completed" as const,
        startedAt: NOW,
        channels: ["ECG"],
        kind: "auto" as const,
      };
      return [
        await ctx.db.insert("sessions", { ...row, organizationId: w.orgA }),
        await ctx.db.insert("sessions", {
          ...row,
          machineId: w.machine,
          organizationId: w.orgA,
          startedAt: NOW - 1,
        }),
      ];
    });

    const page = await as(w.t, "orgAdmin").query(
      api.sessions.listSessionsPaginated,
      { paginationOpts: { numItems: 10, cursor: null } },
    );
    expect(page.page.map((r) => r._id)).toEqual([own]);
    const listed = await as(w.t, "orgAdmin").query(
      api.sessions.listSessions,
      {},
    );
    expect(listed.map((r) => r._id)).toEqual([own]);
    // The Anheart admin reads both rows as they are.
    const all = await as(w.t, "admin").query(api.sessions.listSessions, {});
    expect(all.map((r) => r._id)).toEqual([mixed, own]);
  });

  it("gives a gestionnaire only the machines they manage in the organisation they act in", async () => {
    const w = await seedWorld(modules);
    const ids = await w.t.run(async (ctx) => {
      // manager is also linked to centre B's machine, and to a machine that
      // no longer exists.
      const gone = await ctx.db.insert("machines", {
        organizationId: w.orgA,
        name: "Removed machine",
        apiKey: "synthetic-hash",
        status: "offline",
        lastHeartbeat: NOW,
        createdAt: NOW,
      });
      for (const [machineId, organizationId] of [
        [w.orgBMachine, w.orgB],
        [gone, w.orgA],
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
      const insert = (
        machineId: Id<"machines">,
        organizationId: Id<"organizations">,
      ) =>
        ctx.db.insert("sessions", {
          organizationId,
          machineId,
          status: "completed",
          startedAt: NOW,
          channels: ["ECG"],
          kind: "auto",
        });
      const own = await insert(w.machine, w.orgA);
      const foreign = await insert(w.orgBMachine, w.orgB);
      const orphan = await insert(gone, w.orgA);
      await ctx.db.delete(gone);
      return { own, foreign, orphan };
    });

    const page = await as(w.t, "manager").query(
      api.sessions.listSessionsPaginated,
      { paginationOpts: { numItems: 10, cursor: null } },
    );
    expect(page.page.map((r) => r._id)).toEqual([ids.own]);
    // A session whose machine is gone is listed to the Anheart admin only,
    // as `getSession` serves it to them only.
    const orgAdminList = await as(w.t, "orgAdmin").query(
      api.sessions.listSessions,
      {},
    );
    expect(orgAdminList.map((r) => r._id)).toEqual([ids.own]);
    const adminList = await as(w.t, "admin").query(
      api.sessions.listSessions,
      {},
    );
    expect(adminList.map((r) => r._id)).toEqual([
      ids.orphan,
      ids.foreign,
      ids.own,
    ]);
    expect(adminList[0].machineName).toBe("Unknown");
  });
});
