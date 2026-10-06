/// <reference types="vite/client" />
/**
 * The server and the dashboard judge the freshness of a machine's live state
 * on one threshold, `LIVE_FRESH_MS` of lib/training.ts. If the two ever
 * disagreed, a page could show as live a state the server already withholds.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import { LIVE_FRESH_MS } from "../lib/training";
import {
  now,
  trainingFixture as seedTraining,
} from "./trainingPrivacy.fixtures";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
]);

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(now);
});
afterEach(() => {
  vi.useRealTimers();
});

/** What the manager of the machine is told about a state of the given age. */
async function managerSees(ageMs: number) {
  const f = await seedTraining(modules);
  await f.t.run((ctx) =>
    ctx.db.patch(f.machineId, {
      live: { ...f.live, updatedAt: now - ageMs },
    }),
  );
  const manager = f.t.withIdentity({ subject: "manager" });
  const card = await manager.query(api.training.getMachineLive, {
    machineId: f.machineId,
  });
  const list = await manager.query(api.training.listLaunchableMachines, {});
  return { card, listed: list.find((m) => m._id === f.machineId) };
}

describe("live state threshold shared with the dashboard", () => {
  it("serves a state younger than LIVE_FRESH_MS as fresh", async () => {
    const { card, listed } = await managerSees(LIVE_FRESH_MS - 1);
    expect(card).toMatchObject({ stale: false });
    expect(listed?.live).not.toBeNull();
  });

  it("withholds or marks stale a state older than LIVE_FRESH_MS", async () => {
    const { card, listed } = await managerSees(LIVE_FRESH_MS + 1);
    expect(card).toMatchObject({ stale: true });
    expect(listed?.live).toBeNull();
  });
});
