/**
 * convex-test 0.0.60 counts returned documents, not rows rejected by a filter.
 * The merged sources have no database filter: every scanned session counts
 * towards documentsRead. The unfiltered centre scan proves the budget bites.
 * For pagination, maximumRowsRead counts before filters; page cuts and cursor
 * traversal prove the scan cap, independently of the documentsRead counter.
 */
import { beforeAll, describe, expect, it } from "vitest";
import type { FunctionReturnType } from "convex/server";
import { api } from "./_generated/api";
import { as } from "./test.setup";
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
      expect(
        await reader.query(api.sessions.getCompletedSessionsForUser, {}),
      ).toEqual([]);
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
            .slice(0, limit)
            .map((row) => row.id),
        );
      }
    });
  },
);

describe("ANH-227 short lists: default and previous reports", () => {
  it("keeps the default and previous reports lists exact under the read budget", async () => {
    const w = await boundsWorld(420, true);
    await expectBudgetBites(w);
    const expected = expectedForManager(w, w.seeded);
    const reader = as(w.t, "manager");
    const listed = await reader.query(api.sessions.listSessions, {});
    expect(listed.map((row) => row._id)).toEqual(
      expected.slice(0, 50).map((row) => row.id),
    );
    const completed = await reader.query(
      api.sessions.getCompletedSessionsForUser,
      {},
    );
    expect(completed.map((row) => row._id)).toEqual(
      expected
        .filter((row) => row.status === "completed")
        .slice(0, 100)
        .map((row) => row.id),
    );
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

  it.each([undefined, 5000])(
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

  it("honours a lower client cap, including the native split boundaries", async () => {
    await expectBudgetBites(w);
    const pages = await allPages(w, { maximumRowsRead: 73 });
    expect(pages).toHaveLength(Math.ceil(HISTORY_SIZE / 73));
    expect(pages.flatMap((page) => page.page)).toEqual([]);
    for (const page of pages.slice(0, -1))
      expect(page.pageStatus).toBe("SplitRequired");

    const first = pages[0];
    const reader = as(w.t, "manager");
    const left = await reader.query(api.sessions.listSessionsPaginated, {
      paginationOpts: {
        numItems: 50,
        cursor: null,
        endCursor: first.splitCursor,
        maximumRowsRead: 73,
      },
    });
    const right = await reader.query(api.sessions.listSessionsPaginated, {
      paginationOpts: {
        numItems: 50,
        cursor: first.splitCursor ?? null,
        endCursor: first.continueCursor,
        maximumRowsRead: 73,
      },
    });
    expect([...left.page, ...right.page]).toEqual(first.page);
    expect(left.pageStatus).not.toBe("SplitRequired");
    expect(right.pageStatus).not.toBe("SplitRequired");
    expect(right.continueCursor).toBe(first.continueCursor);
  });
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
