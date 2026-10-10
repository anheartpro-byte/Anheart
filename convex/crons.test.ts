/// <reference types="vite/client" />
/**
 * ANH-132: the `check-offline-machines` cron (`convex/crons.ts` ->
 * `internal.machines.checkOfflineMachines`). A machine with no heartbeat for
 * 90 s becomes `offline`, whether it was `online` or `in_session`; a machine
 * still beating is left alone.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { convexTest } from "convex-test";
import schema from "./schema";
import { internal } from "./_generated/api";
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
    expect(result.markedOfflineCount).toBe(2);

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
