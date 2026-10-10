/// <reference types="vite/client" />
/**
 * The server and the dashboard judge the freshness of a machine's live state
 * on one threshold, `LIVE_FRESH_MS` of lib/training.ts. If the two ever
 * disagreed, a page could show as live a state the server already withholds.
 *
 * The dashboard never reads the clock of the computer it runs on to tell an
 * age: every answer that carries a date to be aged carries the server's clock
 * too (`serverNow`), and the last sign of life of a session is dated by the
 * server when it receives it. The date the machine wrote in that point
 * (`lastMeasuredAt`) is served next to it: a point received this instant may
 * have been measured long ago.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { LIVE_FRESH_MS } from "../lib/training";
import {
  addSession,
  as,
  configureAnheartOrganization,
  seedWorld,
  type World,
} from "./test.setup";
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

  const HOUR = 3_600_000;

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

  /** One batch of the machine's queue: a point per second from `firstT`. */
  const resent = (firstT: number, count = 300) =>
    Array.from({ length: count }, (_, second) => point(firstT + second * 1000));

  /** A session of the fixture's machine, without any telemetry yet. */
  async function sessionOf(
    f: Fixture,
    fields: {
      status: "pending" | "active" | "completed" | "failed";
      origin?: "remote" | "local";
      startedAt: number;
    },
  ): Promise<Id<"sessions">> {
    return await f.t.run(async (ctx) => {
      const machine = await ctx.db.get(f.machineId);
      return await ctx.db.insert("sessions", {
        // Like every session, it belongs to its machine's organisation.
        organizationId: machine?.organizationId,
        machineId: f.machineId,
        userId: f.rider,
        channels: ["ECG"],
        kind: "auto",
        ...fields,
      });
    });
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
        // The machine's own date for that point, served as it wrote it.
        lastMeasuredAt: now + 15_000 + machineClockMs,
        serverNow: now + 75_000,
      });
    },
  );

  it("tells the reception of a point from its measurement: a batch resent after a link loss is received now, measured long ago", async () => {
    // Every clock is right. The link was lost for an hour during the session:
    // the machine resends what it queued, 300 points at a time, oldest first.
    const f = await seedTraining(modules);
    const sessionId = await sessionOf(f, {
      status: "active",
      origin: "remote",
      startedAt: now - 2 * HOUR,
    });
    await f.t.mutation(internal.training.storeTelemetry, {
      machineId: f.machineId,
      sessionId,
      points: resent(now - HOUR),
    });
    vi.setSystemTime(now + 2000);
    expect(await read(f, sessionId)).toMatchObject({
      // Received 2 s ago...
      lastSignalAt: now,
      // ...but its newest point was measured 55 minutes ago.
      lastMeasuredAt: now - HOUR + 299_000,
      serverNow: now + 2000,
    });
  });

  it("follows the measurement date batch after batch while the machine's queue drains", async () => {
    const f = await seedTraining(modules);
    const sessionId = await sessionOf(f, {
      status: "active",
      origin: "remote",
      startedAt: now - HOUR,
    });
    // A ten-minute loss: 600 points queued, then one more every second.
    const lossStart = now - 600_000;
    const told = [];
    for (const [sentAfterMs, firstT, count] of [
      [0, lossStart, 300],
      [5000, lossStart + 300_000, 300],
      [10_000, lossStart + 600_000, 10],
    ] as const) {
      vi.setSystemTime(now + sentAfterMs);
      await f.t.mutation(internal.training.storeTelemetry, {
        machineId: f.machineId,
        sessionId,
        points: resent(firstT, count),
      });
      const session = await read(f, sessionId);
      told.push({
        receivedAgoMs: Date.now() - (session?.lastSignalAt ?? 0),
        measuredAgoMs: Date.now() - (session?.lastMeasuredAt ?? 0),
      });
    }
    expect(told).toEqual([
      { receivedAgoMs: 0, measuredAgoMs: 301_000 },
      { receivedAgoMs: 0, measuredAgoMs: 6000 },
      { receivedAgoMs: 0, measuredAgoMs: 1000 },
    ]);
  });

  it("serves both dates for a session started at the machine and registered after the link returns", async () => {
    // The session ran for an hour without a link: the machine registers it
    // now, with the start it dated itself, then resends its queue.
    const f = await seedTraining(modules);
    const sessionId = await f.t.mutation(
      internal.training.registerLocalSession,
      {
        machineId: f.machineId,
        localRef: "local-late",
        kind: "manual",
        startedAt: now - HOUR,
        operatorName: "Operator",
      },
    );
    expect(await read(f, sessionId)).toMatchObject({
      startedAt: now - HOUR,
      lastSignalAt: now,
      lastMeasuredAt: null,
    });
    vi.setSystemTime(now + 5000);
    await f.t.mutation(internal.training.storeTelemetry, {
      machineId: f.machineId,
      sessionId,
      points: resent(now - HOUR),
    });
    expect(await read(f, sessionId)).toMatchObject({
      lastSignalAt: now + 5000,
      lastMeasuredAt: now - HOUR + 299_000,
      serverNow: now + 5000,
    });
  });

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
    expect(await read(f, sessionId)).toMatchObject({
      lastSignalAt: now + 5000,
      lastMeasuredAt: now + 5000,
    });
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
      lastMeasuredAt: null,
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
        lastMeasuredAt: null,
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
        lastMeasuredAt: null,
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
      lastMeasuredAt: now,
      serverNow: now,
    });
    expect(await asked("outsider")).toBeNull();
    expect(await asked("otherManager")).toBeNull();
  });
});

describe("the server's dates of a machine and of a session stay in their organisation", () => {
  afterEach(() => {
    configureAnheartOrganization(null);
  });

  /** Centre B's machine in a session that sends: every date these queries serve. */
  async function centreBSends() {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.orgBMachine,
      userId: w.orgBPatient,
      status: "active",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(w.orgBMachine, {
        live: {
          runMode: "seance",
          phase: "hold",
          motorRpm: 1200,
          outputRpm: 24,
          setpointMotorRpm: 1200,
          gLoad: 1.2,
          safetyAction: "none",
          sessionId,
          updatedAt: now,
        },
      }),
    );
    await w.t.mutation(internal.training.storeTelemetry, {
      machineId: w.orgBMachine,
      sessionId,
      points: [
        {
          t: now,
          elapsedS: 15,
          phase: "hold",
          motorRpm: 1200,
          outputRpm: 24,
          setpointMotorRpm: 1200,
          gLoad: 1.2,
          safetyAction: "none",
        },
      ],
    });
    return { w, sessionId };
  }

  /** Everything the six queries tell `actor` about centre B's machine and session. */
  async function toldTo(
    w: World,
    sessionId: Id<"sessions">,
    actor: Parameters<typeof as>[1],
  ) {
    const me = as(w.t, actor);
    const ofCentreB = <T extends { _id: Id<"machines"> }>(machines: T[]) =>
      machines.filter((m) => m._id === w.orgBMachine);
    return {
      live: await me.query(api.training.getMachineLive, {
        machineId: w.orgBMachine,
      }),
      session: await me.query(api.training.getTrainingSession, { sessionId }),
      machine: await me.query(api.machines.getMachine, {
        machineId: w.orgBMachine,
      }),
      launchable: ofCentreB(
        await me.query(api.training.listLaunchableMachines, {}),
      ),
      listed: ofCentreB(await me.query(api.machines.listMachines, {})),
      managed: ofCentreB(
        await me.query(api.machines.getMachinesForGestionnaire, {
          gestionnaireId: w.orgBManager,
        }),
      ),
    };
  }

  it("serves them to the machine's own manager", async () => {
    const { w, sessionId } = await centreBSends();
    expect(await toldTo(w, sessionId, "orgBManager")).toMatchObject({
      live: { serverNow: now, live: { updatedAt: now } },
      session: { serverNow: now, lastSignalAt: now, lastMeasuredAt: now },
      machine: { serverNow: now, lastHeartbeat: now },
      launchable: [{ serverNow: now, lastHeartbeat: now }],
      listed: [{ serverNow: now, lastHeartbeat: now }],
      managed: [{ serverNow: now, lastHeartbeat: now }],
    });
  });

  it.each([
    "orgAdmin",
    "manager",
    "otherManager",
    "patient",
    "stranger",
  ] as const)(
    "serves none of them to %s of another organisation",
    async (actor) => {
      const { w, sessionId } = await centreBSends();
      expect(await toldTo(w, sessionId, actor)).toEqual({
        live: null,
        session: null,
        machine: null,
        launchable: [],
        listed: [],
        managed: [],
      });
    },
  );
});
