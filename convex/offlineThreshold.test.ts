/// <reference types="vite/client" />
/**
 * The job that marks a silent machine offline follows `LIVE_FRESH_MS` of
 * lib/training.ts, the threshold the dashboard and the live queries use. With
 * a number of its own it could keep "online" a machine every page already
 * shows offline, or the reverse, the day the threshold changes.
 *
 * The shared constant is replaced here by another value: whatever does not
 * follow it keeps the 90 s it had and fails.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import {
  now,
  trainingFixture as seedTraining,
} from "./trainingPrivacy.fixtures";

/** A threshold nothing else in the repository uses. */
const SHARED_THRESHOLD_MS = 30_000;

vi.mock("../lib/training", async (original) => ({
  ...(await original<typeof import("../lib/training")>()),
  LIVE_FRESH_MS: 30_000,
}));

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

/** The fixture's machine, last heard `silentMs` ago, after the job has run. */
async function afterSilence(silentMs: number) {
  const f = await seedTraining(modules);
  vi.setSystemTime(now + silentMs);
  const report = await f.t.mutation(
    internal.machines.checkOfflineMachines,
    {},
  );
  const machine = await f.t.run((ctx) => ctx.db.get(f.machineId));
  const card = await f.t
    .withIdentity({ subject: "manager" })
    .query(api.training.getMachineLive, { machineId: f.machineId });
  return { report, status: machine?.status, stale: card?.stale };
}

describe("offline job on the threshold shared with the dashboard", () => {
  it("leaves a machine alone up to the shared threshold", async () => {
    const { report, status, stale } = await afterSilence(SHARED_THRESHOLD_MS);
    expect(report.markedOfflineCount).toBe(0);
    expect(status).toBe("in_session");
    expect(stale).toBe(false);
  });

  it("marks a machine offline past the shared threshold, when the live query calls its state stale", async () => {
    const { report, status, stale } = await afterSilence(
      SHARED_THRESHOLD_MS + 1,
    );
    // Both machines of the fixture were last heard at the same instant.
    expect(report.markedOfflineCount).toBe(2);
    expect(status).toBe("offline");
    expect(stale).toBe(true);
  });
});
