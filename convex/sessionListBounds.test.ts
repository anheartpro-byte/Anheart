/**
 * convex-test 0.0.60 counts returned documents, not rows rejected by a filter.
 * The recorder proves the merged sources use narrow indexes without filters:
 * every scanned session counts towards documentsRead. The unfiltered centre
 * scan proves the budget bites.
 * For pagination, maximumRowsRead counts before filters; page cuts and cursor
 * traversal prove the scan cap, independently of the documentsRead counter.
 */
import { beforeAll, describe, expect, it } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "./_generated/api";
import { getCurrentUserOrThrow } from "./lib/auth";
import {
  newestReadableSessions,
  readableSessions,
} from "./lib/readableSessions";
import { as } from "./test.setup";
import { recordReads } from "./readRecorder.fixtures";
import {
  boundsWorld,
  expectedForManager,
  HISTORY_SIZE,
} from "./sessionListBounds.fixtures";

type BoundsWorld = Awaited<ReturnType<typeof boundsWorld>>;

async function expectBudgetBites(w: BoundsWorld) {
  await expect(
    w.t.run(async (ctx) =>
      ctx.db
        .query("sessions")
        .withIndex("by_organization_and_started", (q) =>
          q.eq("organizationId", w.orgA),
        )
        .collect(),
    ),
  ).rejects.toThrow("Scanned too many documents");
}

describe("ANH-227 short lists: bounded source structure", () => {
  describe.each([
    { actor: "manager", sparse: false, machineCount: 0 },
    { actor: "otherManager", sparse: false, machineCount: 0 },
    { actor: "manager", sparse: true, machineCount: 2 },
  ] as const)("$actor with $machineCount managed machines", (scenario) => {
    let w: BoundsWorld;
    beforeAll(async () => {
      w = await boundsWorld(80, scenario.sparse);
    });

    it.each([undefined, "completed", "active"] as const)(
      "takes only bounded machine and rider ranges when status is %s",
      async (status) => {
        const limit = 10;
        const reads = await as(w.t, scenario.actor).run(async (ctx) => {
          const caller = await getCurrentUserOrThrow(ctx);
          const recorded = recordReads(ctx);
          await newestReadableSessions(recorded.ctx, caller, { status }, limit);
          return recorded.reads;
        });
        const sources = reads.filter((read) => read.table === "sessions");
        const machineIndex =
          status === undefined
            ? "by_organization_and_machine_and_started"
            : "by_organization_and_machine_and_status_and_started";
        const riderIndex =
          status === undefined
            ? "by_organization_and_user_and_started"
            : "by_organization_and_user_and_status_and_started";
        expect(sources.map((source) => source.index).sort()).toEqual(
          [
            ...Array.from(
              { length: scenario.machineCount },
              () => machineIndex,
            ),
            riderIndex,
          ].sort(),
        );
        for (const source of sources) {
          expect(source.calls).toEqual([{ method: "take", limit }]);
        }
      },
    );

    it.each([undefined, "completed", "active"] as const)(
      "records the centre scan and filter of the previous path when status is %s",
      async (status) => {
        const limit = 10;
        const reads = await as(w.t, scenario.actor).run(async (ctx) => {
          const caller = await getCurrentUserOrThrow(ctx);
          const recorded = recordReads(ctx);
          await (
            await readableSessions(recorded.ctx, caller, { status })
          ).take(limit);
          return recorded.reads;
        });
        expect(reads.filter((read) => read.table === "sessions")).toEqual([
          {
            table: "sessions",
            index:
              status === undefined
                ? "by_organization_and_started"
                : "by_organization_and_status_and_started",
            calls: [{ method: "filter" }, { method: "take", limit }],
          },
        ]);
      },
    );
  });
});

describe("ANH-227 short lists: a manager with no readable history", () => {
  let w: BoundsWorld;
  beforeAll(async () => {
    w = await boundsWorld(80);
  });

  it.each(["manager", "otherManager"] as const)(
    "returns empty short lists under a read budget for %s",
    async (actor) => {
      await expectBudgetBites(w);
      const reader = as(w.t, actor);
      for (const filters of [
        {},
        { limit: 10 },
        { status: "completed" as const },
      ]) {
        expect(await reader.query(api.sessions.listSessions, filters)).toEqual(
          [],
        );
      }
    },
  );
});

describe.each([1, 5, 10, 50, 100])(
  "ANH-227 short lists: merge limit %i",
  (limit) => {
    let w: BoundsWorld;
    beforeAll(async () => {
      w = await boundsWorld(3 * limit + 120, true);
    });

    it("merges two machines and a rider with and without status under the read budget", async () => {
      await expectBudgetBites(w);
      const expected = expectedForManager(w, w.seeded);
      for (const status of [undefined, "completed", "active"] as const) {
        const result = await as(w.t, "manager").query(
          api.sessions.listSessions,
          { limit, status },
        );
        expect(result.map((row) => row._id)).toEqual(
          expected
            .filter((row) => status === undefined || row.status === status)
            // A limit over 50 is bounded to 50 (`listSessions`).
            .slice(0, Math.min(limit, 50))
            .map((row) => row.id),
        );
      }
    });
  },
);

describe("ANH-227 short lists: default list", () => {
  it("keeps the default list exact under the read budget", async () => {
    const w = await boundsWorld(420, true);
    await expectBudgetBites(w);
    const expected = expectedForManager(w, w.seeded);
    const reader = as(w.t, "manager");
    const listed = await reader.query(api.sessions.listSessions, {});
    expect(listed.map((row) => row._id)).toEqual(
      expected.slice(0, 50).map((row) => row.id),
    );
  });

  it("bounds the limit a caller asks for to 1..50, under the read budget", async () => {
    const w = await boundsWorld(420, true);
    const expected = expectedForManager(w, w.seeded).map((row) => row.id);
    const reader = as(w.t, "manager");
    const ids = async (limit: number) =>
      (await reader.query(api.sessions.listSessions, { limit })).map(
        (row) => row._id,
      );

    expect(await ids(10_000)).toEqual(expected.slice(0, 50));
    expect(await ids(10)).toEqual(expected.slice(0, 10));
    expect(await ids(0)).toEqual(expected.slice(0, 1));
    expect(await ids(-5)).toEqual(expected.slice(0, 1));
    // `take` refuses what is not a whole number: floored, NaN the default.
    expect(await ids(2.5)).toEqual(expected.slice(0, 2));
    expect(await ids(0.5)).toEqual(expected.slice(0, 1));
    expect(await ids(Number.NaN)).toEqual(expected.slice(0, 50));
    expect(await ids(Number.POSITIVE_INFINITY)).toEqual(expected.slice(0, 50));
  });
});

async function allPages(
  w: BoundsWorld,
  options: {
    maximumRowsRead?: number;
    status?: "completed";
  },
) {
  const pages: FunctionReturnType<typeof api.sessions.listSessionsPaginated>[] =
    [];
  let cursor: string | null = null;
  for (let i = 0; i < HISTORY_SIZE; i++) {
    const page: FunctionReturnType<typeof api.sessions.listSessionsPaginated> =
      await as(w.t, "manager").query(api.sessions.listSessionsPaginated, {
        status: options.status,
        paginationOpts: {
          numItems: 50,
          cursor,
          maximumRowsRead: options.maximumRowsRead,
        },
      });
    pages.push(page);
    if (page.isDone) return pages;
    expect(page.continueCursor).not.toBe(cursor);
    cursor = page.continueCursor;
  }
  throw new Error("Pagination never ends");
}

describe("ANH-227 pagination: server scan cap", () => {
  let w: BoundsWorld;
  beforeAll(async () => {
    w = await boundsWorld(1100);
  });

  it.each([undefined, 5000, 0, -1, 0.9, NaN, Infinity])(
    "caps empty pages when the client maximum is %s",
    async (maximumRowsRead) => {
      await expectBudgetBites(w);
      for (const status of [undefined, "completed"] as const) {
        const pages = await allPages(w, { maximumRowsRead, status });
        const size = status === undefined ? HISTORY_SIZE : HISTORY_SIZE / 2;
        expect(pages).toHaveLength(Math.ceil(size / 1000));
        expect(pages.flatMap((page) => page.page)).toEqual([]);
        for (const page of pages.slice(0, -1)) {
          expect(page.isDone).toBe(false);
          expect(page.pageStatus).toBe("SplitRequired");
          expect(page.splitCursor).toEqual(expect.any(String));
        }
      }
    },
  );

  it("honours a client cap of one row on the admin page", async () => {
    const first = await as(w.t, "admin").query(
      api.sessions.listSessionsPaginated,
      {
        paginationOpts: {
          numItems: 50,
          cursor: null,
          maximumRowsRead: 1,
        },
      },
    );
    expect(first.page).toHaveLength(1);
    expect(first.pageStatus).toBe("SplitRequired");
    expect(first.continueCursor).toEqual(expect.any(String));
    expect(first.isDone).toBe(false);
  });

  it.each([73, 73.9])(
    "honours a lower client cap of %s, including the native split boundaries",
    async (maximumRowsRead) => {
      await expectBudgetBites(w);
      const pages = await allPages(w, { maximumRowsRead });
      expect(pages).toHaveLength(Math.ceil(HISTORY_SIZE / 73));
      expect(pages.flatMap((page) => page.page)).toEqual([]);
      for (const page of pages.slice(0, -1))
        expect(page.pageStatus).toBe("SplitRequired");

      const adminPage = await as(w.t, "admin").query(
        api.sessions.listSessionsPaginated,
        {
          paginationOpts: { numItems: 3000, cursor: null, maximumRowsRead },
        },
      );
      expect(adminPage.page).toHaveLength(73);

      const first = pages[0];
      const reader = as(w.t, "manager");
      const left = await reader.query(api.sessions.listSessionsPaginated, {
        paginationOpts: {
          numItems: 50,
          cursor: null,
          endCursor: first.splitCursor,
          maximumRowsRead,
        },
      });
      const right = await reader.query(api.sessions.listSessionsPaginated, {
        paginationOpts: {
          numItems: 50,
          cursor: first.splitCursor ?? null,
          endCursor: first.continueCursor,
          maximumRowsRead,
        },
      });
      expect([...left.page, ...right.page]).toEqual(first.page);
      expect(left.pageStatus).not.toBe("SplitRequired");
      expect(right.pageStatus).not.toBe("SplitRequired");
      expect(right.continueCursor).toBe(first.continueCursor);
    },
  );
});

describe("ANH-227 pagination: sparse readable sessions", () => {
  let w: BoundsWorld;
  beforeAll(async () => {
    w = await boundsWorld(1100, true);
  });

  it("keeps every sparse readable session in order across the cuts", async () => {
    await expectBudgetBites(w);
    for (const status of [undefined, "completed"] as const) {
      const pages = await allPages(w, { status, maximumRowsRead: 5000 });
      expect(pages.length).toBeGreaterThan(1);
      expect(pages.flatMap((page) => page.page.map((row) => row._id))).toEqual(
        expectedForManager(w, w.seeded)
          .filter((row) => status === undefined || row.status === status)
          .map((row) => row.id),
      );
    }
  });

  it("imposes the cap on the Anheart admin too", async () => {
    const adminWorld = await boundsWorld(2100, true);
    await expectBudgetBites(adminWorld);
    const first = await as(adminWorld.t, "admin").query(
      api.sessions.listSessionsPaginated,
      {
        paginationOpts: { numItems: 3000, cursor: null, maximumRowsRead: 5000 },
      },
    );
    expect(first.page).toHaveLength(1000);
    expect(first.pageStatus).toBe("SplitRequired");
    expect(first.isDone).toBe(false);
  });
});
