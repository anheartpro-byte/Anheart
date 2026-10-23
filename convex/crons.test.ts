/// <reference types="vite/client" />
/**
 * ANH-132: the `check-offline-machines` cron (`convex/crons.ts` ->
 * `internal.machines.checkOfflineMachines`). A machine with no heartbeat for
 * 90 s becomes `offline`, whether it was `online` or `in_session`; a machine
 * still beating is left alone.
 *
 * ANH-231: the training sessions without news are marked by another cron,
 * `interrupt-silent-sessions` (`internal.training.interruptSilentSessions`,
 * convex/interruptedSessions.test.ts), so that marking them can never keep
 * this one from marking a machine offline.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "./schema";
import { internal } from "./_generated/api";
import crons from "./crons";
import { modules, NOW } from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-132 check-offline-machines cron", () => {
  it("marks stale machines offline, including one in a session, and spares a fresh one", async () => {
    const t = convexTest(schema, modules);
    const ids = await t.run(async (ctx) => {
      const mk = (status: "online" | "in_session", lastHeartbeat: number) =>
        ctx.db.insert("machines", {
          name: `m-${status}-${lastHeartbeat}`,
          apiKey: "synthetic-hash",
          status,
          lastHeartbeat,
          createdAt: NOW,
        });
      return {
        staleOnline: await mk("online", NOW - 95_000),
        staleInSession: await mk("in_session", NOW - 90_001),
        fresh: await mk("online", NOW),
      };
    });

    const result = await t.mutation(internal.machines.checkOfflineMachines, {});
    expect(result).toEqual({ checkedCount: 3, markedOfflineCount: 2 });

    const read = async (id: keyof typeof ids) =>
      (await t.run((ctx) => ctx.db.get(ids[id])))?.status;
    expect(await read("staleOnline")).toBe("offline");
    expect(await read("staleInSession")).toBe("offline");
    expect(await read("fresh")).toBe("online");
  });

  it("leaves a machine exactly at the 90 s boundary online", async () => {
    const t = convexTest(schema, modules);
    const id = await t.run((ctx) =>
      ctx.db.insert("machines", {
        name: "boundary",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW - 90_000, // cutoff is `< now - 90000`, so this is kept
        createdAt: NOW,
      }),
    );
    await t.mutation(internal.machines.checkOfflineMachines, {});
    expect((await t.run((ctx) => ctx.db.get(id)))?.status).toBe("online");
  });
});

describe("ANH-231 the offline check and the marking of silent sessions are two jobs", () => {
  it("runs each every minute, apart, the marking from its first page", () => {
    const jobs = (
      crons as unknown as {
        crons: Record<
          string,
          { name: string; args: unknown[]; schedule: unknown }
        >;
      }
    ).crons;
    expect(jobs["check-offline-machines"]).toEqual({
      name: "machines:checkOfflineMachines",
      args: [{}],
      schedule: { type: "interval", minutes: 1 },
    });
    expect(jobs["interrupt-silent-sessions"]).toEqual({
      name: "training:interruptSilentSessions",
      args: [{ cursor: null }],
      schedule: { type: "interval", minutes: 1 },
    });
  });

  it("leaves to the other job an active session the server has had no news of for an hour", async () => {
    const t = convexTest(schema, modules);
    const hourAgo = NOW - 60 * 60_000;
    vi.setSystemTime(hourAgo);
    const sessionId = await t.run(async (ctx) => {
      const machineId = await ctx.db.insert("machines", {
        name: "silent",
        apiKey: "synthetic-hash",
        status: "in_session" as const,
        lastHeartbeat: hourAgo,
        createdAt: hourAgo,
      });
      return await ctx.db.insert("sessions", {
        machineId,
        status: "active" as const,
        startedAt: hourAgo,
        channels: ["ECG"],
        kind: "manual" as const,
        origin: "local" as const,
      });
    });
    vi.setSystemTime(NOW);
    const statusOf = async () =>
      (await t.run((ctx) => ctx.db.get(sessionId)))?.status;

    expect(
      await t.mutation(internal.machines.checkOfflineMachines, {}),
    ).toEqual({ checkedCount: 1, markedOfflineCount: 1 });
    expect(await statusOf()).toBe("active");

    expect(
      await t.mutation(internal.training.interruptSilentSessions, {
        cursor: null,
      }),
    ).toEqual({ marked: 1, done: true });
    expect(await statusOf()).toBe("interrupted");
  });
});
