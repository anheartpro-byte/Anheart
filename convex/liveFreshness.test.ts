/// <reference types="vite/client" />
/**
 * The server and the dashboard judge the freshness of a machine's live state
 * on one threshold, `LIVE_FRESH_MS` of lib/training.ts. If the two ever
 * disagreed, a page could show as live a state the server already withholds.
 *
 * The dashboard never reads the clock of the computer it runs on to tell an
 * age: every answer that carries a date to be aged carries the server's clock
 * too (`serverNow`), and the last sign of life of a session is dated by the
 * server when it receives it, never by the machine.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
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

type Fixture = Awaited<ReturnType<typeof seedTraining>>;

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

describe("the server's clock in every answer that carries a date to be aged", () => {
  /** The machine was last heard at `now`; the manager asks 42 s later. */
  const LATER = now + 42_000;

  async function askedLater() {
    const f = await seedTraining(modules);
    vi.setSystemTime(LATER);
    return { f, manager: f.t.withIdentity({ subject: "manager" }) };
  }

  it("getMachineLive: the state's date and the clock it is aged on", async () => {
    const { f, manager } = await askedLater();
    const card = await manager.query(api.training.getMachineLive, {
      machineId: f.machineId,
    });
    expect(card).toMatchObject({
      serverNow: LATER,
      live: { updatedAt: now },
    });
  });

  it("listLaunchableMachines: the last signal, the state's date and the clock", async () => {
    const { f, manager } = await askedLater();
    const list = await manager.query(api.training.listLaunchableMachines, {});
    expect(list.find((m) => m._id === f.machineId)).toMatchObject({
      lastHeartbeat: now,
      serverNow: LATER,
      live: { updatedAt: now },
    });
  });

  it("listLaunchableMachines: the last signal of a machine whose state is withheld", async () => {
    const { f, manager } = await askedLater();
    vi.setSystemTime(now + LIVE_FRESH_MS);
    const list = await manager.query(api.training.listLaunchableMachines, {});
    expect(list.find((m) => m._id === f.machineId)).toMatchObject({
      lastHeartbeat: now,
      serverNow: now + LIVE_FRESH_MS,
      live: null,
    });
  });

  it("getMachine: the last signal and the clock", async () => {
    const { f, manager } = await askedLater();
    const machine = await manager.query(api.machines.getMachine, {
      machineId: f.machineId,
    });
    expect(machine).toMatchObject({ lastHeartbeat: now, serverNow: LATER });
  });

  it("listMachines: the last signal and the clock, for each machine", async () => {
    const { f } = await askedLater();
    const machines = await f.t
      .withIdentity({ subject: "admin" })
      .query(api.machines.listMachines, {});
    expect(machines).toHaveLength(2);
    for (const machine of machines) {
      expect(machine).toMatchObject({ lastHeartbeat: now, serverNow: LATER });
    }
  });

  it("getMachinesForGestionnaire: the last signal and the clock", async () => {
    const { f, manager } = await askedLater();
    const machines = await manager.query(
      api.machines.getMachinesForGestionnaire,
      {},
    );
    expect(machines).toEqual([
      expect.objectContaining({
        _id: f.machineId,
        lastHeartbeat: now,
        serverNow: LATER,
      }),
    ]);
  });
});

describe("the last sign of life of a session, dated by the server", () => {
  /** How far the machine's clock is from the server's. */
  const MACHINE_CLOCKS = [
    ["on time", 0],
    ["10 min ahead", 600_000],
    ["10 min behind", -600_000],
  ] as const;

  const point = (t: number) => ({
    t,
    elapsedS: 15,
    phase: "hold",
    bpm: 137,
    motorRpm: 1200,
    outputRpm: 24,
    setpointMotorRpm: 1200,
    gLoad: 1.2,
    safetyAction: "none",
  });

  /** A session of the fixture's machine, without any telemetry yet. */
  async function sessionOf(
    f: Fixture,
    fields: {
      status: "pending" | "active" | "completed" | "failed";
      origin?: "remote" | "local";
      startedAt: number;
    },
  ): Promise<Id<"sessions">> {
    return await f.t.run((ctx) =>
      ctx.db.insert("sessions", {
        machineId: f.machineId,
        userId: f.rider,
        channels: ["ECG"],
        kind: "auto",
        ...fields,
      }),
    );
  }

  const read = (f: Fixture, sessionId: Id<"sessions">) =>
    f.t
      .withIdentity({ subject: "manager" })
      .query(api.training.getTrainingSession, { sessionId });

  it.each(MACHINE_CLOCKS)(
    "is the reception of the last point, whatever date the machine wrote in it (machine clock %s)",
    async (_name, machineClockMs) => {
      const f = await seedTraining(modules);
      const sessionId = await sessionOf(f, {
        status: "active",
        origin: "remote",
        startedAt: now,
      });
      // The machine sends a point every 5 s, dated on its own clock.
      for (const afterMs of [5000, 10_000, 15_000]) {
        vi.setSystemTime(now + afterMs);
        await f.t.mutation(internal.training.storeTelemetry, {
          machineId: f.machineId,
          sessionId,
          points: [point(now + afterMs + machineClockMs)],
        });
      }
      // Then it is silent: the manager's page asks 60 s later.
      vi.setSystemTime(now + 75_000);
      expect(await read(f, sessionId)).toMatchObject({
        lastSignalAt: now + 15_000,
        serverNow: now + 75_000,
      });
    },
  );

  it("is the reception of the batch for every point of one batch", async () => {
    const f = await seedTraining(modules);
    const sessionId = await sessionOf(f, {
      status: "active",
      origin: "remote",
      startedAt: now,
    });
    vi.setSystemTime(now + 5000);
    await f.t.mutation(internal.training.storeTelemetry, {
      machineId: f.machineId,
      sessionId,
      points: [1, 2, 3, 4, 5].map((s) => point(now + s * 1000)),
    });
    expect((await read(f, sessionId))?.lastSignalAt).toBe(now + 5000);
  });

  it("is the start the server dated, while a session launched from the dashboard has sent no point", async () => {
    const f = await seedTraining(modules);
    const sessionId = await sessionOf(f, {
      status: "pending",
      origin: "remote",
      startedAt: now,
    });
    vi.setSystemTime(now + 30_000);
    await f.t.mutation(internal.training.markTrainingStarted, {
      machineId: f.machineId,
      sessionId,
    });
    vi.setSystemTime(now + 34_000);
    expect(await read(f, sessionId)).toMatchObject({
      startedAt: now + 30_000,
      lastSignalAt: now + 30_000,
      serverNow: now + 34_000,
    });
  });

  it.each(MACHINE_CLOCKS)(
    "is the registration the server dated, not the start the machine wrote, while a session started at the machine has sent no point (machine clock %s)",
    async (_name, machineClockMs) => {
      const f = await seedTraining(modules);
      vi.setSystemTime(now + 2000);
      const sessionId = await f.t.mutation(
        internal.training.registerLocalSession,
        {
          machineId: f.machineId,
          localRef: "local-1",
          kind: "manual",
          startedAt: now + machineClockMs,
          operatorName: "Operator",
        },
      );
      vi.setSystemTime(now + 9000);
      expect(await read(f, sessionId)).toMatchObject({
        startedAt: now + machineClockMs,
        lastSignalAt: now + 2000,
        serverNow: now + 9000,
      });
    },
  );

  it.each(["pending", "completed", "failed"] as const)(
    "is absent for a session that is not active (%s)",
    async (status) => {
      const f = await seedTraining(modules);
      const sessionId = await sessionOf(f, {
        status,
        origin: "remote",
        startedAt: now,
      });
      await f.t.mutation(internal.training.storeTelemetry, {
        machineId: f.machineId,
        sessionId,
        points: [point(now)],
      });
      expect(await read(f, sessionId)).toMatchObject({
        lastSignalAt: null,
        serverNow: now,
      });
    },
  );

  it("is told to the rider too, and to nobody who may not read the session", async () => {
    const f = await seedTraining(modules);
    const asked = (subject: string) =>
      f.t
        .withIdentity({ subject })
        .query(api.training.getTrainingSession, { sessionId: f.sessionId });
    expect(await asked("rider")).toMatchObject({
      lastSignalAt: now,
      serverNow: now,
    });
    expect(await asked("outsider")).toBeNull();
    expect(await asked("otherManager")).toBeNull();
  });
});
