/// <reference types="vite/client" />
/**
 * ANH-216 EX-1: what closes, on the server, an active session whose machine
 * never sends its end (console killed, record unreadable, machine never
 * switched on again), and what never does.
 *
 * - the job `interrupt-silent-sessions` marks `interrupted` (end unknown) an
 *   active training session the server has heard nothing of for
 *   `SESSION_SILENCE_MS`: no heartbeat of its machine naming it, no point, no
 *   event received, no registration. Never `completed` nor `failed`;
 * - a session that runs is never closed by the server: while its machine
 *   names it, it is not marked; marked while its link is down, it is still
 *   `active: true` for the machine, which is therefore never asked to stop, a
 *   stop asked for on the site still reaches it, and the next heartbeat that
 *   names it makes it active again;
 * - the machine that comes back late with the real end closes it with that
 *   end, and what it sends of it meanwhile is stored;
 * - who may read or stop an interrupted session: the same people as for an
 *   active one, never an account of another organisation; and a machine
 *   revives, or speaks for, its own sessions only.
 *
 * ANH-231: that job is not the one that marks a machine offline
 * (`check-offline-machines`), and reads `INTERRUPT_BATCH` sessions per
 * transaction, the next page scheduled at once. Under the limits of a
 * deployed Convex, a world of more silent sessions than one transaction can
 * read still has its machines marked offline, and all its silent sessions
 * marked, page after page, and only them (EX-3). A launch is refused while
 * the machine is silent or runs the session again, and no longer once it is
 * back without naming it (EX-5).
 *
 * Everything runs in memory with `convex-test`, the server's clock faked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { SESSION_SILENCE_MS } from "../lib/training";
import { machineHeaders } from "./machineAuth.fixtures";
import { INTERRUPT_BATCH } from "./training";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  ORG_B_ACTORS,
  seedMachineWorld,
  seedWorld,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

const MINUTE = 60_000;
const SECOND = 1_000;

const machineWorld = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof machineWorld>>;

/** A request of a machine of the world, with its key and the contract it speaks. */
async function post(
  w: MachineWorld,
  path: string,
  body: unknown,
  key: string = w.machineKey,
) {
  const response = await w.t.fetch(path, {
    method: "POST",
    headers: { ...machineHeaders(key), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  expect(response.status).toBe(200);
  return (await response.json()) as Record<string, unknown>;
}

/** A heartbeat, naming the session the machine runs, or none. */
const heartbeat = (
  w: MachineWorld,
  running?: Id<"sessions">,
  key: string = w.machineKey,
) =>
  post(
    w,
    "/api/machine/heartbeat",
    running === undefined ? {} : { activeSessionId: running },
    key,
  );

/** What the machine is told when it asks whether its session should stop. */
async function statusAsked(w: MachineWorld, sessionId: Id<"sessions">) {
  const response = await w.t.fetch(
    `/api/machine/training/status?sessionId=${sessionId}`,
    { method: "GET", headers: machineHeaders(w.machineKey) },
  );
  expect(response.status).toBe(200);
  return (await response.json()) as Record<string, unknown>;
}

/** A session started at the machine, registered now, the machine saying its age. */
async function declared(w: MachineWorld, localRef = "record-1") {
  const { sessionId } = await post(w, "/api/machine/training/local", {
    localRef,
    kind: "manual",
    startedAt: Date.now(),
    sessionAgeMs: 0,
    operatorName: "op-0123456789abcdef",
  });
  return sessionId as Id<"sessions">;
}

/** One 1 Hz point, `second` seconds into a session the machine dated `start`. */
const point = (start: number, second: number) => ({
  t: start + second * SECOND,
  elapsedS: second,
  phase: "hold",
  bpm: 140,
  motorRpm: 1000,
  outputRpm: 20,
  setpointMotorRpm: 1000,
  gLoad: 1.1,
  safetyAction: "none",
});

/**
 * The two jobs, as the crons run them at `at` on the server's clock: the
 * offline check, then the marking of silent sessions, of which these small
 * worlds hold one page. Returns what the marking reports.
 */
async function jobAt(w: { t: MachineWorld["t"] }, at: number) {
  vi.setSystemTime(at);
  await w.t.mutation(internal.machines.checkOfflineMachines, {});
  const report = await w.t.mutation(internal.training.interruptSilentSessions, {
    cursor: null,
  });
  expect(report.done).toBe(true);
  return report;
}

const sessionOf = (w: { t: MachineWorld["t"] }, id: Id<"sessions">) =>
  w.t.run((ctx) => ctx.db.get(id));

const machineOf = (w: MachineWorld, id: Id<"machines">) =>
  w.t.run((ctx) => ctx.db.get(id));

describe("ANH-216 EX-1 a session the server has no news of is marked interrupted, end unknown", () => {
  it("marks it ten minutes after the last heartbeat that named it, not before, and never as ended", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await heartbeat(w, sessionId);

    const before = await jobAt(w, NOW + SESSION_SILENCE_MS - SECOND);
    expect(before.marked).toBe(0);
    expect((await sessionOf(w, sessionId))?.status).toBe("active");

    const after = await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);
    expect(after.marked).toBe(1);
    const session = await sessionOf(w, sessionId);
    expect(session?.status).toBe("interrupted");
    expect(session?.interruptedAt).toBe(NOW + SESSION_SILENCE_MS + SECOND);
    expect(session?.endedAt).toBeUndefined();
    expect(session?.endReason).toBeUndefined();
    expect((await machineOf(w, w.machine))?.status).toBe("offline");
  });

  it("marks it once: a later pass neither counts it again nor moves its date", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    const again = await jobAt(w, NOW + 3 * SESSION_SILENCE_MS);
    expect(again.marked).toBe(0);
    expect((await sessionOf(w, sessionId))?.interruptedAt).toBe(
      NOW + SESSION_SILENCE_MS + SECOND,
    );
  });

  it("marks a session whose machine is back but no longer names it (console restarted, record unreadable)", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await heartbeat(w, sessionId);
    for (let minute = 1; minute <= 11; minute++) {
      vi.setSystemTime(NOW + minute * MINUTE);
      await heartbeat(w);
      await jobAt(w, NOW + minute * MINUTE + SECOND);
    }

    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
    expect((await machineOf(w, w.machine))?.status).toBe("online");
  });

  it("dates a launch from the dashboard from the machine's confirmation, not from the launch", async () => {
    const w = await machineWorld();
    const sessionId = await w.t.run((ctx) =>
      ctx.db.insert("sessions", {
        organizationId: w.organizationId,
        machineId: w.machine,
        userId: w.patient,
        startedById: w.patient,
        status: "pending" as const,
        startedAt: NOW,
        channels: ["ECG"],
        kind: "auto" as const,
        origin: "remote" as const,
        profileId: "p1",
        subjectHrMax: 180,
        subjectAge: 36,
      }),
    );
    // Within the five minutes a launch may wait for its machine (ANH-144).
    const confirmed = NOW + 4 * MINUTE;
    vi.setSystemTime(confirmed);
    await post(w, "/api/machine/training/start", { sessionId });

    await jobAt(w, confirmed + SESSION_SILENCE_MS - SECOND);
    expect((await sessionOf(w, sessionId))?.status).toBe("active");
    await jobAt(w, confirmed + SESSION_SILENCE_MS + SECOND);
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
  });

  it("dates a session started at the machine from its registration, not from the start its clock wrote", async () => {
    // A console that says no age, its clock a day ahead: the start it wrote
    // is stored as the session's start, and is no news of the session.
    const w = await machineWorld();
    const { sessionId } = await post(w, "/api/machine/training/local", {
      localRef: "record-ahead",
      kind: "manual",
      startedAt: NOW + 24 * 60 * MINUTE,
      operatorName: "op-0123456789abcdef",
    });
    const id = sessionId as Id<"sessions">;
    expect((await sessionOf(w, id))?.startedAt).toBe(NOW + 24 * 60 * MINUTE);

    await jobAt(w, NOW + SESSION_SILENCE_MS - SECOND);
    expect((await sessionOf(w, id))?.status).toBe("active");
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);
    expect((await sessionOf(w, id))?.status).toBe("interrupted");
  });

  it("leaves alone what is not an active training session", async () => {
    const w = await machineWorld();
    const ids = await w.t.run(async (ctx) => {
      const insert = (
        status: "pending" | "active" | "completed" | "failed",
        kind?: "auto",
      ) =>
        ctx.db.insert("sessions", {
          organizationId: w.organizationId,
          machineId: w.machine,
          status,
          startedAt: NOW,
          channels: ["ECG"],
          kind,
        });
      return {
        legacy: await insert("active"),
        pending: await insert("pending", "auto"),
        completed: await insert("completed", "auto"),
        failed: await insert("failed", "auto"),
      };
    });

    const report = await jobAt(w, NOW + 10 * SESSION_SILENCE_MS);

    expect(report.marked).toBe(0);
    expect((await sessionOf(w, ids.legacy))?.status).toBe("active");
    expect((await sessionOf(w, ids.pending))?.status).toBe("pending");
    expect((await sessionOf(w, ids.completed))?.status).toBe("completed");
    expect((await sessionOf(w, ids.failed))?.status).toBe("failed");
  });
});

describe("ANH-216 EX-1 a session that runs is never closed by the server", () => {
  it("never marks a session its machine keeps naming, however long it runs", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    for (let minute = 0; minute <= 180; minute++) {
      vi.setSystemTime(NOW + minute * MINUTE);
      if (minute % 9 === 0) await heartbeat(w, sessionId);
      const report = await jobAt(w, NOW + minute * MINUTE + SECOND);
      expect(report.marked).toBe(0);
    }
    expect((await sessionOf(w, sessionId))?.status).toBe("active");
  });

  it.each(["points", "events"] as const)(
    "counts %s received as news of the session, as a machine catching up sends them",
    async (what) => {
      const w = await machineWorld();
      const sessionId = await declared(w);
      const received = NOW + 9 * MINUTE;
      vi.setSystemTime(received);
      if (what === "points") {
        await post(w, "/api/machine/training/telemetry", {
          sessionId,
          points: [point(NOW, 1)],
        });
      } else {
        await post(w, "/api/machine/training/events", {
          sessionId,
          events: [
            { seq: 0, t: NOW + SECOND, kind: "phase", detail: "hold", actor: "system" },
          ],
        });
      }

      await jobAt(w, received + SESSION_SILENCE_MS - SECOND);
      expect((await sessionOf(w, sessionId))?.status).toBe("active");
      await jobAt(w, received + SESSION_SILENCE_MS + SECOND);
      expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
    },
  );

  it("still tells the machine its interrupted session is active, so the machine is never asked to stop", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    expect(await statusAsked(w, sessionId)).toMatchObject({
      status: "interrupted",
      active: true,
      stopRequested: false,
    });
  });

  it("makes it active again at the next heartbeat that names it, and keeps it so", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    const back = NOW + SESSION_SILENCE_MS + 5 * MINUTE;
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    vi.setSystemTime(back);
    await heartbeat(w, sessionId);

    const session = await sessionOf(w, sessionId);
    expect(session?.status).toBe("active");
    expect(session?.interruptedAt).toBeUndefined();
    expect((await machineOf(w, w.machine))?.status).toBe("in_session");
    const report = await jobAt(w, back + MINUTE);
    expect(report.marked).toBe(0);
    expect((await sessionOf(w, sessionId))?.status).toBe("active");
  });

  it("passes a stop asked for on the site to the machine when it asks again", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    await w.admin.mutation(api.training.requestStop, { sessionId });

    expect(await statusAsked(w, sessionId)).toMatchObject({
      active: true,
      stopRequested: true,
    });
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
  });
});

describe("ANH-216 EX-1 the machine that comes back late with the real end", () => {
  it("stores what it sends of the interrupted session, then closes it with its end and its date", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    const back = NOW + 2 * 60 * MINUTE;
    vi.setSystemTime(back);
    const outcome = await post(w, "/api/machine/training/telemetry", {
      sessionId,
      points: [point(NOW, 600), point(NOW, 1200)],
    });
    expect(outcome).toMatchObject({ stored: 2, rejected: 0 });
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");

    await post(w, "/api/machine/training/end", {
      sessionId,
      failed: true,
      reason: "interrupted",
      endedAt: NOW + 1200 * SECOND,
    });

    const session = await sessionOf(w, sessionId);
    expect(session?.status).toBe("failed");
    expect(session?.endReason).toBe("interrupted");
    expect(session?.endedAt).toBe(NOW + 1200 * SECOND);
    expect(session?.interruptedAt).toBeUndefined();
    expect(await statusAsked(w, sessionId)).toMatchObject({ active: false });
  });

  it("closes it as completed when that is how it ended", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    vi.setSystemTime(NOW + 60 * MINUTE);
    await post(w, "/api/machine/training/end", {
      sessionId,
      failed: false,
      reason: "operator_stop: fini",
      endedAt: NOW + 30 * MINUTE,
    });

    const session = await sessionOf(w, sessionId);
    expect(session?.status).toBe("completed");
    expect(session?.endedAt).toBe(NOW + 30 * MINUTE);
  });
});

describe("ANH-216 EX-1 a machine speaks for its own sessions only", () => {
  it("does not revive another machine's session by naming it", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);

    vi.setSystemTime(NOW + SESSION_SILENCE_MS + MINUTE);
    await heartbeat(w, sessionId, w.otherKey);

    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
  });

  it("does not keep another machine's session from being marked by naming it", async () => {
    const w = await machineWorld();
    const sessionId = await declared(w);
    for (let minute = 1; minute <= 11; minute++) {
      vi.setSystemTime(NOW + minute * MINUTE);
      await heartbeat(w, sessionId, w.otherKey);
    }
    await jobAt(w, NOW + 11 * MINUTE + SECOND);

    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
  });
});

describe("ANH-216 EX-1 who may read and stop an interrupted session", () => {
  async function interruptedWorld() {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "interrupted",
      kind: "auto",
    });
    await w.t.run((ctx) => ctx.db.patch(sessionId, { interruptedAt: NOW }));
    return { w, sessionId };
  }

  it.each(["patient", "manager", "orgAdmin", "admin"] as const)(
    "%s reads it as interrupted, may stop it, and the stop is recorded",
    async (actor) => {
      const { w, sessionId } = await interruptedWorld();
      const shown = await as(w.t, actor).query(
        api.training.getTrainingSession,
        { sessionId },
      );
      expect(shown).toMatchObject({
        status: "interrupted",
        interruptedAt: NOW,
        canStop: true,
        lastSignalAt: null,
        lastMeasuredAt: null,
      });

      await as(w.t, actor).mutation(api.training.requestStop, { sessionId });

      const session = await sessionOf(w, sessionId);
      expect(session?.stopRequestedAt).toBe(NOW);
      expect(session?.status).toBe("interrupted");
    },
  );

  it.each(ORG_B_ACTORS)(
    "%s of another organisation neither reads nor stops it",
    async (actor) => {
      const { w, sessionId } = await interruptedWorld();
      expect(
        await as(w.t, actor).query(api.training.getTrainingSession, {
          sessionId,
        }),
      ).toBeNull();
      await expect(
        as(w.t, actor).mutation(api.training.requestStop, { sessionId }),
      ).rejects.toThrow(/session_not_found/);
      expect((await sessionOf(w, sessionId))?.stopRequestedAt).toBeUndefined();
    },
  );

  it.each(["stranger", "otherManager"] as const)(
    "%s of the same organisation, without access to it, may not stop it",
    async (actor) => {
      const { w, sessionId } = await interruptedWorld();
      await expect(
        as(w.t, actor).mutation(api.training.requestStop, { sessionId }),
      ).rejects.toThrow(/session_not_managed/);
    },
  );

  it.each([
    ["patient", true],
    ["manager", true],
    ["orgAdmin", true],
    ["admin", true],
    ["otherManager", false],
    ["stranger", false],
    ["orgBAdmin", false],
    ["orgBManager", false],
    ["orgBPatient", false],
  ] as const)(
    "lists a session the job marked to %s (%s) under its own status and under all, never as active",
    async (actor, readable) => {
      const w = await seedWorld(modules);
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);
      expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");

      const reader = as(w.t, actor);
      const page = async (status?: "active" | "interrupted") =>
        (
          await reader.query(api.sessions.listSessionsPaginated, {
            ...(status === undefined ? {} : { status }),
            paginationOpts: { numItems: 50, cursor: null },
          })
        ).page.map((row) => row._id);
      const short = async (status?: "active" | "interrupted") =>
        (
          await reader.query(
            api.sessions.listSessions,
            status === undefined ? {} : { status },
          )
        ).map((row) => row._id);

      const shown = readable ? [sessionId] : [];
      expect(await page("interrupted")).toEqual(shown);
      expect(await page()).toEqual(shown);
      expect(await short("interrupted")).toEqual(shown);
      expect(await short()).toEqual(shown);
      expect(await page("active")).toEqual([]);
      expect(await short("active")).toEqual([]);
    },
  );
});

// ---------------------------------------------------------------------------
// ANH-231 EX-3: the marking is a job of its own, a bounded page per transaction
// ---------------------------------------------------------------------------

/** Silent sessions enough for eleven pages of the job, the last one short. */
const MANY = 10 * INTERRUPT_BATCH + 7;
/** The heartbeats the first machine sends, one per 5 s for eight minutes. */
const UNNAMING_BEATS = 96;
/** When every session of these worlds started at `NOW` is due to be marked. */
const DUE = NOW + SESSION_SILENCE_MS + SECOND;

type LimitedWorld = Awaited<ReturnType<typeof seedWorld>>;

/** A world under the limits of one transaction of a deployed Convex. */
const limitedWorld = () => seedWorld(modules, { transactionLimits: true });

/**
 * `count` active training sessions on `machineId`, registered at the machine
 * now, of which the server hears nothing afterwards.
 */
function insertSilentSessions(
  w: LimitedWorld,
  count: number,
  machineId: Id<"machines"> = w.machine,
) {
  return w.t.run(async (ctx) => {
    const machine = await ctx.db.get(machineId);
    const ids: Id<"sessions">[] = [];
    for (let rank = 0; rank < count; rank++) {
      ids.push(
        await ctx.db.insert("sessions", {
          organizationId: machine?.organizationId,
          machineId,
          status: "active",
          startedAt: Date.now(),
          channels: ["ECG"],
          kind: rank % 2 === 0 ? "auto" : "manual",
          origin: "local",
        }),
      );
    }
    return ids;
  });
}

/**
 * `MANY` silent sessions on the first machine, whose console, restarted on
 * records it cannot read, beat every 5 s for eight minutes from `NOW`
 * without naming any of them, then fell silent: each of them costs the job
 * every one of those heartbeats.
 */
async function silentWorld() {
  const w = await limitedWorld();
  const sessions = await insertSilentSessions(w, MANY);
  for (let beat = 1; beat <= UNNAMING_BEATS; beat++) {
    vi.setSystemTime(NOW + beat * 5 * SECOND);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
    });
  }
  return { w, sessions };
}

const interrupt = (w: Pick<LimitedWorld, "t">, cursor: string | null) =>
  w.t.mutation(internal.training.interruptSilentSessions, { cursor });

/** The pages of the job handed to the scheduler, in the order they were. */
const scheduledPages = (w: Pick<LimitedWorld, "t">) =>
  w.t.run(async (ctx) =>
    (await ctx.db.system.query("_scheduled_functions").collect()).filter(
      (job) => job.name.includes("interruptSilentSessions"),
    ),
  );

/** The cursor the last page scheduled hands over to the next one. */
async function nextCursor(w: Pick<LimitedWorld, "t">): Promise<string> {
  const pages = await scheduledPages(w);
  const [args] = pages[pages.length - 1].args as { cursor: string }[];
  return args.cursor;
}

const rowsOf = (w: Pick<LimitedWorld, "t">, ids: Id<"sessions">[]) =>
  w.t.run((ctx) => Promise.all(ids.map((id) => ctx.db.get(id))));

const statusesOf = async (w: Pick<LimitedWorld, "t">, ids: Id<"sessions">[]) =>
  new Set((await rowsOf(w, ids)).map((row) => row?.status));

describe("ANH-231 EX-3 a machine without news is marked offline, however many sessions are silent", () => {
  beforeEach(() => {
    // Every timer faked: what a job schedules waits for the test to run it.
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  it("is a world one transaction cannot read: the heartbeats its silent sessions cost are over the limit", async () => {
    const { w } = await silentWorld();
    vi.setSystemTime(DUE);

    await expect(
      w.t.run(async (ctx) => {
        const since = Date.now() - SESSION_SILENCE_MS;
        const active = await ctx.db
          .query("sessions")
          .withIndex("by_status_and_organization", (q) =>
            q.eq("status", "active"),
          )
          .collect();
        for (const session of active) {
          await ctx.db
            .query("machine_heartbeats")
            .withIndex("by_machine_and_timestamp", (q) =>
              q.eq("machineId", session.machineId).gt("timestamp", since),
            )
            .collect();
        }
      }),
    ).rejects.toThrow(/Scanned too many documents/);
  });

  it("marks every silent machine offline, each with its alert, and changes none of the sessions", async () => {
    const { w, sessions } = await silentWorld();
    vi.setSystemTime(DUE);

    expect(
      await w.t.mutation(internal.machines.checkOfflineMachines, {}),
    ).toEqual({ checkedCount: 3, markedOfflineCount: 3 });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    const machines = await w.t.run(async (ctx) =>
      Promise.all(
        [w.machine, w.otherMachine, w.orgBMachine].map((id) => ctx.db.get(id)),
      ),
    );
    expect(machines.map((machine) => machine?.status)).toEqual([
      "offline",
      "offline",
      "offline",
    ]);
    const alerts = await w.t.run((ctx) =>
      ctx.db.query("notifications").collect(),
    );
    // Its gestionnaire and its organisation's admin, for each machine.
    const alerted = (machineId: Id<"machines">) =>
      alerts
        .filter(
          (row) =>
            row.machineId === machineId && row.type === "machine_offline",
        )
        .map((row) => row.userId)
        .sort();
    expect(alerted(w.machine)).toEqual([w.manager, w.orgAdmin].sort());
    expect(alerted(w.otherMachine)).toEqual(
      [w.otherManager, w.orgAdmin].sort(),
    );
    expect(alerted(w.orgBMachine)).toEqual([w.orgBManager, w.orgBAdmin].sort());

    const rows = await rowsOf(w, sessions);
    expect(rows).toHaveLength(MANY);
    for (const row of rows) {
      expect(row?.status).toBe("active");
      expect(row?.interruptedAt).toBeUndefined();
    }
    expect(await scheduledPages(w)).toEqual([]);
  });
});

describe("ANH-231 EX-3 the job marks the silent sessions a bounded page per transaction", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  it("marks one page, schedules the next at once, and its pages, each on its own clock, mark them all", async () => {
    const { w, sessions } = await silentWorld();
    vi.setSystemTime(DUE);

    expect(await interrupt(w, null)).toEqual({
      marked: INTERRUPT_BATCH,
      done: false,
    });
    const first = await rowsOf(w, sessions);
    expect(first.filter((row) => row?.status === "interrupted")).toHaveLength(
      INTERRUPT_BATCH,
    );
    expect(await scheduledPages(w)).toMatchObject([
      {
        state: { kind: "pending" },
        scheduledTime: DUE,
        args: [{ cursor: expect.any(String) }],
      },
    ]);

    // The next pages run later: each dates what it marks on its own clock.
    const later = DUE + 5 * SECOND;
    vi.setSystemTime(later);
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    const rows = await rowsOf(w, sessions);
    expect(rows.every((row) => row?.status === "interrupted")).toBe(true);
    expect(rows.filter((row) => row?.interruptedAt === DUE)).toHaveLength(
      INTERRUPT_BATCH,
    );
    expect(rows.filter((row) => row?.interruptedAt === later)).toHaveLength(
      MANY - INTERRUPT_BATCH,
    );
    const pages = await scheduledPages(w);
    expect(pages).toHaveLength(Math.ceil(MANY / INTERRUPT_BATCH) - 1);
    expect(pages.map((page) => page.state.kind)).toEqual(
      pages.map(() => "success"),
    );
  });

  it("never marks more than INTERRUPT_BATCH sessions in one transaction, page after page, by the cursor each hands over", async () => {
    const { w, sessions } = await silentWorld();
    vi.setSystemTime(DUE);

    const reports = [await interrupt(w, null)];
    while (!reports[reports.length - 1].done) {
      reports.push(await interrupt(w, await nextCursor(w)));
    }

    expect(reports.map((report) => report.marked)).toEqual([
      ...Array.from({ length: 10 }, () => INTERRUPT_BATCH),
      MANY - 10 * INTERRUPT_BATCH,
    ]);
    expect(reports.map((report) => report.done)).toEqual([
      ...Array.from({ length: 10 }, () => false),
      true,
    ]);
    expect(await statusesOf(w, sessions)).toEqual(new Set(["interrupted"]));
  });

  it("stops on a page that finds no active session left: it marks and schedules nothing", async () => {
    const w = await limitedWorld();
    const sessions = await insertSilentSessions(w, INTERRUPT_BATCH + 3);
    vi.setSystemTime(DUE);
    expect(await interrupt(w, null)).toEqual({
      marked: INTERRUPT_BATCH,
      done: false,
    });
    // The machine sends the end of the last three before the next page runs.
    await w.t.run(async (ctx) => {
      for (const id of sessions.slice(INTERRUPT_BATCH)) {
        await ctx.db.patch(id, { status: "completed", endedAt: DUE });
      }
    });

    expect(await interrupt(w, await nextCursor(w))).toEqual({
      marked: 0,
      done: true,
    });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    expect((await scheduledPages(w)).map((page) => page.state.kind)).toEqual([
      "success",
    ]);
    expect(await statusesOf(w, sessions.slice(INTERRUPT_BATCH))).toEqual(
      new Set(["completed"]),
    );
  });

  it("finds nothing to do in a deployment without an active session", async () => {
    const w = await limitedWorld();
    expect(await interrupt(w, null)).toEqual({ marked: 0, done: true });
    expect(await scheduledPages(w)).toEqual([]);
  });
});

describe("ANH-231 EX-3 a session with recent news is never marked, whichever page it falls in", () => {
  beforeEach(() => {
    vi.useRealTimers();
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });

  /** What the server hears of a session after its registration at `NOW`. */
  type Fate =
    | "named"
    | "point"
    | "event"
    | "confirmed"
    | "legacy"
    | "namedByAnother"
    | "silent";
  const fateOf = (rank: number): Fate => {
    const fates: Fate[] = [
      "named",
      "point",
      "event",
      "confirmed",
      "legacy",
      "namedByAnother",
    ];
    return fates[rank % 10] ?? "silent";
  };

  it("leaves active a session named by its machine, with a point, an event, its start confirmed or its registration recent, and marks only the silent ones", async () => {
    const w = await limitedWorld();
    const machines = [w.machine, w.otherMachine, w.orgBMachine];
    const machineOf = (rank: number) => machines[Math.floor(rank / 10) % 3];
    const seeded = await w.t.run(async (ctx) => {
      const rows: { id: Id<"sessions">; fate: Fate; rank: number }[] = [];
      for (let rank = 0; rank < MANY; rank++) {
        const fate = fateOf(rank);
        const machineId = machineOf(rank);
        const machine = await ctx.db.get(machineId);
        const id = await ctx.db.insert("sessions", {
          organizationId: machine?.organizationId,
          machineId,
          status: "active",
          startedAt: NOW,
          channels: ["ECG"],
          kind: fate === "legacy" ? undefined : "auto",
          origin: fate === "confirmed" ? "remote" : "local",
        });
        rows.push({ id, fate, rank });
      }
      return rows;
    });

    // Eight minutes later, the news of each session that has some.
    const heard = NOW + 8 * MINUTE;
    vi.setSystemTime(heard);
    for (const { id, fate, rank } of seeded) {
      const machineId = machineOf(rank);
      if (fate === "named") {
        await w.t.mutation(internal.machines.recordHeartbeat, {
          machineId,
          activeSessionId: id,
        });
      } else if (fate === "namedByAnother") {
        await w.t.mutation(internal.machines.recordHeartbeat, {
          machineId: machines.find((other) => other !== machineId) ?? machineId,
          activeSessionId: id,
        });
      } else if (fate === "point") {
        await w.t.mutation(internal.training.storeTelemetry, {
          machineId,
          sessionId: id,
          points: [point(NOW, 60)],
        });
      } else if (fate === "event") {
        await w.t.mutation(internal.training.storeEvents, {
          machineId,
          sessionId: id,
          events: [
            {
              seq: 0,
              t: NOW + SECOND,
              kind: "phase",
              detail: "hold",
              actor: "system",
            },
          ],
        });
      } else if (fate === "confirmed") {
        // Its start confirmed now: `markTrainingStarted` dates it so.
        await w.t.run((ctx) => ctx.db.patch(id, { startedAt: heard }));
      }
    }
    // Five sessions declared now: their registration is news of them.
    const declared = await insertSilentSessions(w, 5, w.otherMachine);

    vi.setSystemTime(DUE);
    expect(await interrupt(w, null)).toMatchObject({ done: false });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);

    // The chain went through every page: MANY + 5 active sessions.
    expect(await scheduledPages(w)).toHaveLength(
      Math.ceil((MANY + declared.length) / INTERRUPT_BATCH) - 1,
    );
    const rows = await rowsOf(
      w,
      seeded.map((row) => row.id),
    );
    const marked = (fate: Fate) =>
      seeded.filter(
        (row, index) =>
          row.fate === fate && rows[index]?.status === "interrupted",
      ).length;
    const count = (fate: Fate) =>
      seeded.filter((row) => row.fate === fate).length;
    for (const fate of [
      "named",
      "point",
      "event",
      "confirmed",
      "legacy",
    ] as const) {
      expect([fate, marked(fate)]).toEqual([fate, 0]);
    }
    expect(marked("silent")).toBe(count("silent"));
    expect(marked("namedByAnother")).toBe(count("namedByAnother"));
    for (const [index, row] of rows.entries()) {
      if (row?.status === "interrupted") {
        expect(row.interruptedAt).toBe(DUE);
      } else {
        expect(row?.status).toBe("active");
        expect(row?.interruptedAt).toBeUndefined();
        expect(["named", "point", "event", "confirmed", "legacy"]).toContain(
          seeded[index].fate,
        );
      }
    }
    expect(await statusesOf(w, declared)).toEqual(new Set(["active"]));
  });
});

// ---------------------------------------------------------------------------
// ANH-231 EX-5: the link cut for 11 minutes during a session
// ---------------------------------------------------------------------------

describe("ANH-231 EX-5 a launch is refused while the machine is silent or runs the session again", () => {
  /** A launch from the dashboard by the Anheart admin, for the world's patient. */
  const launch = (w: MachineWorld, machineId: Id<"machines">) =>
    w.admin.mutation(api.training.launchAutoSession, {
      machineId,
      profileId: w.profileId,
      userId: w.patient,
    });

  /** The stable code of a refused call (convex/lib/errors.ts), or null. */
  const refusalOf = (call: Promise<unknown>) =>
    call.then(
      () => null,
      (error: unknown) => (error instanceof ConvexError ? error.data : error),
    );

  /**
   * A session launched for the patient and armed by the first machine, which
   * names it once; then nothing from the machine for 11 minutes, both jobs
   * running every minute.
   */
  async function linkCutFor11Minutes() {
    const w = await machineWorld();
    const sessionId = await launch(w, w.machine);
    await post(w, "/api/machine/training/start", { sessionId });
    await heartbeat(w, sessionId);
    for (let minute = 1; minute <= 11; minute++) {
      await jobAt(w, NOW + minute * MINUTE);
    }
    expect((await machineOf(w, w.machine))?.status).toBe("offline");
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
    // The other machine is up: whatever holds the rider is not its state.
    vi.setSystemTime(NOW + 11 * MINUTE + 10 * SECOND);
    await heartbeat(w, undefined, w.otherKey);
    return { w, sessionId };
  }

  const sessionCount = (w: MachineWorld) =>
    w.t.run(async (ctx) => (await ctx.db.query("sessions").collect()).length);

  it("refuses it while the machine is silent, and still once a heartbeat names the session again", async () => {
    const { w, sessionId } = await linkCutFor11Minutes();

    expect(await refusalOf(launch(w, w.machine))).toBe("machine_offline");
    expect(await refusalOf(launch(w, w.otherMachine))).toBe("rider_busy");

    vi.setSystemTime(NOW + 11 * MINUTE + 20 * SECOND);
    await heartbeat(w, sessionId);
    const session = await sessionOf(w, sessionId);
    expect(session?.status).toBe("active");
    expect(session?.interruptedAt).toBeUndefined();
    expect((await machineOf(w, w.machine))?.status).toBe("in_session");

    expect(await refusalOf(launch(w, w.machine))).toBe("machine_busy");
    expect(await refusalOf(launch(w, w.otherMachine))).toBe("rider_busy");
    expect(await sessionCount(w)).toBe(1);
  });

  it("no longer refuses it for that session once the machine is back without naming it", async () => {
    const { w, sessionId } = await linkCutFor11Minutes();

    vi.setSystemTime(NOW + 11 * MINUTE + 20 * SECOND);
    await heartbeat(w);
    expect((await machineOf(w, w.machine))?.status).toBe("online");
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");

    const next = await launch(w, w.machine);
    expect((await sessionOf(w, next))?.status).toBe("pending");
    expect(await sessionCount(w)).toBe(2);
  });
});
