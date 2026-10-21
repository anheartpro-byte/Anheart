import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import { v } from "convex/values";
import type { Doc, Id } from "./_generated/dataModel";
import {
  internalMutation,
  mutation,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import * as notifications from "./notifications";
import { getCurrentUserOrThrow } from "./lib/auth";
import {
  continueMarkAllReadHandler,
  markAllReadHandler,
  notificationVisibility,
  recentVisible,
  unreadCountHandler,
  visibleUnread,
} from "./lib/notificationVisibility";
import {
  LIST_LIMIT,
  MARK_ALL_READ_LIMIT,
  UNREAD_COUNT_LIMIT,
} from "./notifications";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedWorld,
  type World,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

async function addAlerts(
  w: World,
  fields: Pick<Doc<"notifications">, "userId" | "machineId"> & {
    count: number;
    updatedAt?: number;
    organizationId?: Id<"organizations">;
    sessionId?: Id<"sessions">;
  },
) {
  return await w.t.run(async (ctx) => {
    const ids: Id<"notifications">[] = [];
    for (let i = 0; i < fields.count; i++) {
      ids.push(
        await ctx.db.insert("notifications", {
          userId: fields.userId,
          machineId: fields.machineId,
          organizationId: fields.organizationId ?? w.orgA,
          sessionId: fields.sessionId,
          type: "machine_offline",
          count: 1,
          createdAt: NOW,
          updatedAt: (fields.updatedAt ?? NOW) + i,
          read: false,
        }),
      );
    }
    return ids;
  });
}

async function linkMachine(w: World, machineId: Id<"machines">) {
  await w.t.run((ctx) =>
    ctx.db.insert("machine_gestionnaires", {
      organizationId: w.orgA,
      machineId,
      gestionnaireId: w.manager,
      isOwner: false,
      createdAt: NOW,
      createdBy: w.orgAdmin,
    }),
  );
}

async function unlinkMachine(w: World, machineId: Id<"machines">) {
  await w.t.run(async (ctx) => {
    const links = await ctx.db
      .query("machine_gestionnaires")
      .withIndex("by_machine_and_gestionnaire", (q) =>
        q.eq("machineId", machineId).eq("gestionnaireId", w.manager),
      )
      .collect();
    for (const link of links) await ctx.db.delete(link._id);
  });
}

function recordReads<T extends QueryCtx>(ctx: T) {
  const tables: string[] = [];
  let notificationRows = 0;
  function record<TQuery extends object>(query: TQuery, table: string): TQuery {
    return new Proxy(query, {
      get(target, key, receiver) {
        const method: unknown = Reflect.get(target, key, receiver);
        if (typeof method !== "function") return method;
        return (...args: unknown[]) => {
          if (
            table === "notifications" &&
            (key === "filter" || key === "collect")
          ) {
            throw new Error(
              "Notification reads must use bounded unfiltered index ranges",
            );
          }
          const result: unknown = Reflect.apply(method, target, args);
          if (
            (key === "withIndex" || key === "order") &&
            typeof result === "object" &&
            result !== null
          ) {
            return record(result, table);
          }
          if (table === "notifications" && key === "take") {
            return Promise.resolve(result).then((rows: unknown) => {
              if (!Array.isArray(rows))
                throw new Error("Expected notification rows");
              notificationRows += rows.length;
              return rows;
            });
          }
          return result;
        };
      },
    });
  }
  const db = new Proxy(ctx.db, {
    get(target, key, receiver) {
      if (key === "query") {
        return (table: Parameters<QueryCtx["db"]["query"]>[0]) => {
          if (table === "machines" || table === "sessions")
            throw new Error(`Forbidden read: ${table}`);
          tables.push(table);
          return record(target.query(table), table);
        };
      }
      const method: unknown = Reflect.get(target, key, receiver);
      if (key === "get" && typeof method === "function") {
        return (...args: unknown[]) => {
          const id = args.length === 1 ? args[0] : args[1];
          if (typeof id === "string") {
            for (const table of ["machines", "sessions"] as const) {
              if (target.normalizeId(table, id) !== null)
                throw new Error(`Forbidden read: ${table}`);
            }
          }
          return Reflect.apply(method, target, args);
        };
      }
      return method;
    },
  });
  return { ctx: { ...ctx, db }, tables, rowsRead: () => notificationRows };
}

function modulesRecordingBatches(batches: number[]) {
  async function recorded(
    ctx: MutationCtx,
    run: (ctx: MutationCtx) => Promise<{ marked: number }>,
  ) {
    const reads = recordReads(ctx);
    const result = await run(reads.ctx);
    batches.push(reads.rowsRead());
    return result;
  }
  return {
    ...modules,
    "./notifications.ts": async () => ({
      ...notifications,
      markAllRead: mutation({
        args: {},
        handler: (ctx) => recorded(ctx, markAllReadHandler),
      }),
      continueMarkAllRead: internalMutation({
        args: { userId: v.id("users"), organizationId: v.id("organizations") },
        handler: (ctx, args) =>
          recorded(ctx, (ctx) => continueMarkAllReadHandler(ctx, args)),
      }),
    }),
  };
}

describe("ANH-237 visibility before notification limits", () => {
  it("keeps a patient's alerts invisible to every shared read", async () => {
    const w = await seedWorld(modules);
    await addAlerts(w, { userId: w.patient, machineId: w.machine, count: 1 });

    const result = await as(w.t, "patient").run(async (ctx) => {
      const caller = await getCurrentUserOrThrow(ctx);
      const visibility = await notificationVisibility(ctx, caller);
      const row = await ctx.db.query("notifications").first();
      if (row === null) throw new Error("Missing alert");
      return {
        allowed: visibility.allows(row),
        recent: await recentVisible(ctx, caller),
        unread: await visibleUnread(ctx, caller, UNREAD_COUNT_LIMIT),
      };
    });

    expect(result).toEqual({ allowed: false, recent: [], unread: [] });
  });

  it("deduplicates linked machines and refuses a link from another organisation", async () => {
    const w = await seedWorld(modules);
    await linkMachine(w, w.machine);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgB,
        machineId: w.otherMachine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.orgBAdmin,
      }),
    );
    const visible = await addAlerts(w, {
      userId: w.manager,
      machineId: w.machine,
      count: 1,
    });
    const [hidden] = await addAlerts(w, {
      userId: w.manager,
      machineId: w.otherMachine,
      count: 1,
    });
    const manager = as(w.t, "manager");

    const count = await manager.query(api.notifications.unreadCount, {});
    const rows = await manager.query(api.notifications.list, {});

    expect(count).toBe(1);
    expect(rows.map((row) => row._id)).toEqual(visible);
    await expect(
      manager.mutation(api.notifications.markRead, { notificationId: hidden }),
    ).rejects.toThrow(/notification_not_found/);
  });

  it.each(["orgAdmin", "manager"] as const)(
    "counts for %s without reading machines or sessions, even after a heartbeat",
    async (actor) => {
      // Given unread alerts and a recorder that rejects machine and session reads.
      const w = await seedWorld(modules);
      const userId = actor === "orgAdmin" ? w.orgAdmin : w.manager;
      await addAlerts(w, { userId, machineId: w.machine, count: 3 });
      const caller = as(w.t, actor);
      const count = () =>
        caller.run(async (ctx) => {
          const reads = recordReads(ctx);
          const value = await unreadCountHandler(reads.ctx);
          return { value, tables: reads.tables };
        });

      // When the bell counts before and after a rewrite of the live machine document.
      const before = await count();
      await w.t.run((ctx) =>
        ctx.db.patch(w.machine, {
          status: "online",
          lastHeartbeat: NOW + 10_000,
          live: {
            runMode: "seance",
            phase: "hold",
            motorRpm: 1200,
            outputRpm: 24,
            setpointMotorRpm: 1200,
            gLoad: 1.2,
            safetyAction: "none",
            driveState: "operation_enabled",
            updatedAt: NOW + 10_000,
          },
        }),
      );
      const after = await count();

      // Then only stable identity, membership, links and notification tables were read.
      expect(before.value).toBe(3);
      expect(after).toEqual(before);
      expect(before.tables).toContain("notifications");
      expect(
        before.tables.every((table) =>
          [
            "users",
            "organizations",
            "memberships",
            "machine_gestionnaires",
            "notifications",
          ].includes(table),
        ),
      ).toBe(true);
    },
  );

  it.each(["orgAdmin", "manager"] as const)(
    "marks every visible alert for %s in bounded scheduled batches",
    async (actor) => {
      // Given more than two batches of visible alerts and a removed machine's alerts.
      const batches: number[] = [];
      const w = await seedWorld(modulesRecordingBatches(batches));
      const userId = actor === "orgAdmin" ? w.orgAdmin : w.manager;
      await linkMachine(w, w.otherMachine);
      await addAlerts(w, {
        userId,
        machineId: w.machine,
        count: MARK_ALL_READ_LIMIT - 5,
      });
      await addAlerts(w, {
        userId,
        machineId: w.otherMachine,
        count: MARK_ALL_READ_LIMIT + 15,
      });
      const hidden = await addAlerts(w, {
        userId,
        machineId: w.orgBMachine,
        organizationId: w.orgB,
        count: 7,
      });
      const removedMachine = await w.t.run((ctx) =>
        ctx.db.insert("machines", {
          organizationId: w.orgA,
          name: "Removed machine",
          status: "offline",
          lastHeartbeat: NOW,
          createdAt: NOW,
        }),
      );
      await linkMachine(w, removedMachine);
      const removed = await addAlerts(w, {
        userId: w.manager,
        machineId: removedMachine,
        count: 9,
      });
      await unlinkMachine(w, removedMachine);

      // When one click starts marking and all scheduled continuations run.
      const first = await as(w.t, actor).mutation(
        api.notifications.markAllRead,
        {},
      );
      await w.t.finishAllScheduledFunctions(() => vi.runAllTimers());

      // Then each transaction obeyed the cap and hidden alerts stayed unread.
      expect(first).toEqual({ marked: MARK_ALL_READ_LIMIT });
      expect(batches).toEqual([MARK_ALL_READ_LIMIT, MARK_ALL_READ_LIMIT, 10]);
      const rows = await w.t.run((ctx) =>
        ctx.db.query("notifications").collect(),
      );
      expect(
        rows
          .filter(
            (row) =>
              row.userId === userId &&
              row.organizationId === w.orgA &&
              row.machineId !== removedMachine,
          )
          .every((row) => row.read),
      ).toBe(true);
      expect(
        rows
          .filter((row) => [...hidden, ...removed].includes(row._id))
          .every((row) => !row.read),
      ).toBe(true);
    },
  );

  it.each(["role", "link", "membership", "user", "organization"] as const)(
    "rechecks %s access before a scheduled continuation",
    async (change) => {
      // Given a first batch marked and a continuation waiting to run.
      const w = await seedWorld(modules);
      await addAlerts(w, {
        userId: w.manager,
        machineId: w.machine,
        count: MARK_ALL_READ_LIMIT + 1,
      });
      await as(w.t, "manager").mutation(api.notifications.markAllRead, {});
      await w.t.run(async (ctx) => {
        switch (change) {
          case "role":
          case "membership": {
            const membership = await ctx.db
              .query("memberships")
              .withIndex("by_user_and_organization", (q) =>
                q.eq("userId", w.manager).eq("organizationId", w.orgA),
              )
              .unique();
            if (membership === null) throw new Error("Missing membership");
            await ctx.db.patch(
              membership._id,
              change === "role" ? { role: "user" } : { active: false },
            );
            break;
          }
          case "link": {
            const link = await ctx.db
              .query("machine_gestionnaires")
              .withIndex("by_machine_and_gestionnaire", (q) =>
                q.eq("machineId", w.machine).eq("gestionnaireId", w.manager),
              )
              .unique();
            if (link === null) throw new Error("Missing link");
            await ctx.db.delete(link._id);
            break;
          }
          case "user":
            await ctx.db.patch(w.manager, { clerkDeletedAt: NOW });
            break;
          case "organization":
            await ctx.db.patch(w.orgA, { deletedAt: NOW });
            break;
        }
      });

      // When the queued continuation runs after access was removed.
      await w.t.finishAllScheduledFunctions(() => vi.runAllTimers());

      // Then it leaves the remaining unread alert alone.
      const rows = await w.t.run((ctx) =>
        ctx.db.query("notifications").collect(),
      );
      expect(rows.filter((row) => row.read)).toHaveLength(MARK_ALL_READ_LIMIT);
      expect(rows.filter((row) => !row.read)).toHaveLength(1);
    },
  );

  it.each([100, 150])(
    "finds the visible alert behind %i newer alerts of a removed machine",
    async (count) => {
      // Given a removed machine whose newer alerts exceed both display limits.
      const w = await seedWorld(modules);
      await linkMachine(w, w.otherMachine);
      const visible = await addAlerts(w, {
        userId: w.manager,
        machineId: w.machine,
        count: 1,
      });
      await addAlerts(w, {
        userId: w.manager,
        machineId: w.otherMachine,
        count,
        updatedAt: NOW + 100,
      });
      await unlinkMachine(w, w.otherMachine);
      const manager = as(w.t, "manager");

      // When the bell and page read the caller's alerts.
      const unread = await manager.query(api.notifications.unreadCount, {});
      const rows = await manager.query(api.notifications.list, {});

      // Then invisible alerts consume neither limit.
      expect(unread).toBe(1);
      expect(rows.map((row) => row._id)).toEqual(visible);
    },
  );

  it.each(["orgAdmin", "manager"] as const)(
    "keeps both limits and the global newest-first order for %s",
    async (actor) => {
      // Given interleaved alerts of two visible machines.
      const w = await seedWorld(modules);
      await linkMachine(w, w.otherMachine);
      const userId = actor === "orgAdmin" ? w.orgAdmin : w.manager;
      await addAlerts(w, { userId, machineId: w.machine, count: 70 });
      await addAlerts(w, {
        userId,
        machineId: w.otherMachine,
        count: 70,
        updatedAt: NOW + 35,
      });
      const caller = as(w.t, actor);

      // When reading the bell and page.
      const unread = await caller.query(api.notifications.unreadCount, {});
      const rows = await caller.query(api.notifications.list, {});

      // Then the caps apply to visible alerts and ordering crosses machine boundaries.
      const expected = [
        ...Array.from({ length: 70 }, (_, i) => NOW + i),
        ...Array.from({ length: 70 }, (_, i) => NOW + 35 + i),
      ]
        .sort((a, b) => b - a)
        .slice(0, LIST_LIMIT);
      expect(unread).toBe(UNREAD_COUNT_LIMIT);
      expect(rows.map((row) => row.updatedAt)).toEqual(expected);
      expect(new Set(rows.map((row) => row.machineId)).size).toBe(2);
    },
  );

  it("refuses marking an invisible alert as if it did not exist", async () => {
    // Given an unread alert for a machine no longer managed by its recipient.
    const w = await seedWorld(modules);
    const [notificationId] = await addAlerts(w, {
      userId: w.manager,
      machineId: w.machine,
      count: 1,
    });
    await unlinkMachine(w, w.machine);

    // When the former gestionnaire tries to mark it read.
    await expect(
      as(w.t, "manager").mutation(api.notifications.markRead, {
        notificationId,
      }),
    ).rejects.toThrow(/notification_not_found/);

    // Then the hidden alert remains unread.
    expect((await w.t.run((ctx) => ctx.db.get(notificationId)))?.read).toBe(
      false,
    );
  });

  it("keeps deleted-machine alerts readable exactly like their sessions", async () => {
    // Given alerts and a session of a logically deleted machine.
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    for (const userId of [w.orgAdmin, w.manager]) {
      await addAlerts(w, { userId, machineId: w.machine, sessionId, count: 1 });
    }
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: true }));

    // When its organisation admin and linked gestionnaire read it.
    for (const actor of ["orgAdmin", "manager"] as const) {
      const caller = as(w.t, actor);
      const rows = await caller.query(api.notifications.list, {});

      // Then both alerts and sessions remain accessible, with the deleted flag.
      expect(rows).toHaveLength(1);
      expect(rows[0]).toMatchObject({ machineDeleted: true, sessionId });
      expect(await caller.query(api.notifications.unreadCount, {})).toBe(1);
      expect(
        await caller.query(api.sessions.getSession, { sessionId }),
      ).not.toBeNull();
    }

    // When the gestionnaire loses the link, neither resource is readable.
    await unlinkMachine(w, w.machine);
    const manager = as(w.t, "manager");
    expect(await manager.query(api.notifications.list, {})).toEqual([]);
    expect(await manager.query(api.notifications.unreadCount, {})).toBe(0);
    expect(
      await manager.query(api.sessions.getSession, { sessionId }),
    ).toBeNull();
  });
});
