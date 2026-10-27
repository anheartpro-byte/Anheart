/// <reference types="vite/client" />
/**
 * ANH-240: a console whose 200 to `POST /api/machine/training/start` was lost
 * asks again, and is answered 200, so that the machine and the rider are not
 * held by a session that does not run.
 *
 * - EX-1: a launch from the dashboard already active on the same machine,
 *   whose start this route confirmed and of which the machine has sent
 *   nothing since (no point, no event, no heartbeat naming it, no end): a
 *   repeated confirmation is answered `{armed: true}`, 200 on the route, and
 *   nothing is written: not its dates, not the machine's state, no audit row,
 *   nothing scheduled;
 * - EX-2: every other session is refused as before, with the same code and
 *   the same status, and nothing is written: another machine's
 *   (`session_not_found`), one the machine has sent a point, an event or a
 *   heartbeat naming it of, one asked to stop, ended, failed by the machine,
 *   cancelled, interrupted, started at the machine (`session_not_pending`);
 *   and a launch refused at arming gets the same 409 again;
 * - EX-3 (Convex half): these cases, by the internal mutation and by the
 *   real route with a real machine key.
 *
 * Everything runs in memory with `convex-test`, the server's clock faked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { LIVE_FRESH_MS, SESSION_SILENCE_MS } from "../lib/training";
import { machineHeaders } from "./machineAuth.fixtures";
import { ARMING_REFUSALS } from "./training";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  seedMachineWorld,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

const SECOND = 1_000;
const MINUTE = 60 * SECOND;

const machineWorld = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof machineWorld>>;
type Backend = Pick<MachineWorld, "t">;

/** A request of a machine of the world, with its key and the contract it speaks. */
function post(
  w: MachineWorld,
  path: string,
  body: unknown,
  key: string = w.machineKey,
) {
  return w.t.fetch(path, {
    method: "POST",
    headers: { ...machineHeaders(key), "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

/** POST /api/machine/training/start: its status and its body. */
async function confirm(
  w: MachineWorld,
  body: Record<string, unknown>,
  key: string = w.machineKey,
) {
  const response = await post(w, "/api/machine/training/start", body, key);
  return { status: response.status, body: (await response.json()) as unknown };
}

/** A machine route that must accept what it is sent. */
async function accepted(w: MachineWorld, path: string, body: unknown) {
  const response = await post(w, path, body);
  expect(response.status).toBe(200);
  return (await response.json()) as Record<string, unknown>;
}

/** A launch from the dashboard, made now by the admin for the patient. */
const launch = (w: MachineWorld) =>
  w.admin.mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    profileId: w.profileId,
    userId: w.patient,
  });

/** A launch whose start the first machine confirmed at `at`, saying nothing of its age. */
async function started(w: MachineWorld, at: number = NOW + 2 * SECOND) {
  const sessionId = await launch(w);
  vi.setSystemTime(at);
  expect(await confirm(w, { sessionId })).toEqual({
    status: 200,
    body: { success: true },
  });
  return sessionId;
}

/**
 * Every row a confirmation could write: the sessions, the machines, the audit
 * rows, what the machines sent, and what is scheduled. Whole documents,
 * `_creationTime` included; of a scheduled function, what was scheduled and
 * not how far its run has gone, since the alerts run meanwhile.
 */
const everything = (w: Backend) =>
  w.t.run(async (ctx) => ({
    sessions: await ctx.db.query("sessions").collect(),
    machines: await ctx.db.query("machines").collect(),
    audit: await ctx.db.query("audit_log").collect(),
    telemetry: await ctx.db.query("training_telemetry").collect(),
    events: await ctx.db.query("training_events").collect(),
    heartbeats: await ctx.db.query("machine_heartbeats").collect(),
    scheduled: (
      await ctx.db.system.query("_scheduled_functions").collect()
    ).map(({ _id, _creationTime, name, args, scheduledTime }) => ({
      _id,
      _creationTime,
      name,
      args,
      scheduledTime,
    })),
  }));

const rowOf = <Table extends "sessions" | "machines">(
  w: Backend,
  id: Id<Table>,
) => w.t.run((ctx) => ctx.db.get(id));

/** The refusal the route answers for a session that is no longer pending. */
const notPending = (status: string) => ({
  status: 400,
  body: {
    error: "session_not_pending",
    message: `Session is not pending (status: ${status})`,
  },
});

/** The data of a refused call: the stable code and its words. */
async function refusalOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

/** One 1 Hz point of the session. */
const point = (t: number) => ({
  t,
  elapsedS: 1,
  phase: "hold",
  bpm: 140,
  motorRpm: 1000,
  outputRpm: 20,
  setpointMotorRpm: 1000,
  gLoad: 1.1,
  safetyAction: "none",
});

// ---------------------------------------------------------------------------
// EX-1: a start already confirmed, of which the machine sent nothing
// ---------------------------------------------------------------------------

describe("ANH-240 EX-1 a repeated confirmation of a start the machine has sent nothing of is answered 200", () => {
  it.each([
    {
      name: "the first one said the machine's start and its age",
      first: { startedAt: 1_000_000, sessionAgeMs: 1_500 },
      startedAt: NOW + 2 * SECOND - 1_500,
      machineStartedAt: 1_000_000,
    },
    {
      name: "the first one said neither",
      first: {},
      startedAt: NOW + 2 * SECOND,
      machineStartedAt: undefined,
    },
  ])(
    "answers 200 again and changes nothing, twice ($name)",
    async ({ first, startedAt, machineStartedAt }) => {
      const w = await machineWorld();
      const sessionId = await launch(w);
      vi.setSystemTime(NOW + 2 * SECOND);
      expect(await confirm(w, { sessionId, ...first })).toEqual({
        status: 200,
        body: { success: true },
      });
      const confirmed = await everything(w);
      const session = await rowOf(w, sessionId);
      expect(session?.status).toBe("active");
      expect(session?.startedAt).toBe(startedAt);
      expect(session?.machineStartedAt).toBe(machineStartedAt);
      expect((await rowOf(w, w.machine))?.status).toBe("in_session");
      expect(
        confirmed.audit.filter((row) => row.action === "session.started"),
      ).toHaveLength(1);

      // Later, with another start and another age: either one, read, would
      // date the session differently.
      vi.setSystemTime(NOW + 3 * MINUTE);
      expect(
        await confirm(w, {
          sessionId,
          startedAt: 2_000_000,
          sessionAgeMs: 90 * SECOND,
        }),
      ).toEqual({ status: 200, body: { success: true } });
      expect(await everything(w)).toEqual(confirmed);

      vi.setSystemTime(NOW + 4 * MINUTE);
      expect(await confirm(w, { sessionId })).toEqual({
        status: 200,
        body: { success: true },
      });
      expect(await everything(w)).toEqual(confirmed);
    },
  );

  it("returns {armed: true} from the internal mutation, without a write", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    const arm = (machineStartedAt?: number, sessionAgeMs?: number) =>
      w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
        machineStartedAt,
        sessionAgeMs,
      });
    vi.setSystemTime(NOW + SECOND);
    expect(await arm(1_000_000, 500)).toEqual({ armed: true });
    const confirmed = await everything(w);

    vi.setSystemTime(NOW + 2 * MINUTE);
    expect(await arm(3_000_000, 60 * SECOND)).toEqual({ armed: true });
    expect(await arm()).toEqual({ armed: true });
    expect(await everything(w)).toEqual(confirmed);
    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "active",
      startedAt: NOW + SECOND - 500,
      machineStartedAt: 1_000_000,
    });
  });

  it("does not put back in session a machine a heartbeat has since said online", async () => {
    const w = await machineWorld();
    const sessionId = await started(w);

    // The console never armed: its heartbeat names no session.
    vi.setSystemTime(NOW + 30 * SECOND);
    await accepted(w, "/api/machine/heartbeat", {});
    expect((await rowOf(w, w.machine))?.status).toBe("online");
    const before = await everything(w);

    vi.setSystemTime(NOW + MINUTE);
    expect(await confirm(w, { sessionId })).toEqual({
      status: 200,
      body: { success: true },
    });
    expect(await everything(w)).toEqual(before);
  });

  it("does not put back in session a machine the offline check has since marked offline", async () => {
    const w = await machineWorld();
    const sessionId = await started(w);

    vi.setSystemTime(NOW + LIVE_FRESH_MS + MINUTE);
    await w.t.mutation(internal.machines.checkOfflineMachines, {});
    expect((await rowOf(w, w.machine))?.status).toBe("offline");
    const before = await everything(w);

    vi.setSystemTime(NOW + LIVE_FRESH_MS + 2 * MINUTE);
    expect(await confirm(w, { sessionId })).toEqual({
      status: 200,
      body: { success: true },
    });
    expect(await everything(w)).toEqual(before);
  });

  it("is not held back by a heartbeat that names no session, or another session", async () => {
    const w = await machineWorld();
    const elsewhere = await w.t.run((ctx) =>
      ctx.db.insert("sessions", {
        organizationId: w.organizationId,
        machineId: w.otherMachine,
        status: "completed",
        startedAt: NOW,
        endedAt: NOW + MINUTE,
        channels: ["ECG"],
        kind: "manual",
        origin: "local",
      }),
    );
    const sessionId = await started(w);

    vi.setSystemTime(NOW + 10 * SECOND);
    await accepted(w, "/api/machine/heartbeat", {});
    vi.setSystemTime(NOW + 20 * SECOND);
    await accepted(w, "/api/machine/heartbeat", { activeSessionId: elsewhere });
    const before = await everything(w);
    expect(before.heartbeats).toHaveLength(2);

    vi.setSystemTime(NOW + 30 * SECOND);
    expect(await confirm(w, { sessionId })).toEqual({
      status: 200,
      body: { success: true },
    });
    expect(await everything(w)).toEqual(before);
  });
});

// ---------------------------------------------------------------------------
// EX-2: every other session is refused as before
// ---------------------------------------------------------------------------

describe("ANH-240 EX-2 every other session is refused as before, without a write", () => {
  it.each([
    {
      name: "the machine sent a point of it",
      status: "active",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        accepted(w, "/api/machine/training/telemetry", {
          sessionId,
          points: [point(NOW + 3 * SECOND)],
        }).then((stored) => expect(stored.stored).toBe(1)),
    },
    {
      name: "the machine sent an event of it",
      status: "active",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        accepted(w, "/api/machine/training/events", {
          sessionId,
          events: [
            {
              seq: 1,
              t: NOW + 3 * SECOND,
              kind: "phase",
              detail: "ramp",
              actor: "system",
            },
          ],
        }).then((stored) => expect(stored.stored).toBe(1)),
    },
    {
      name: "a heartbeat of its machine named it",
      status: "active",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        accepted(w, "/api/machine/heartbeat", { activeSessionId: sessionId }),
    },
    {
      name: "a stop was asked for on the site",
      status: "active",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        w.admin.mutation(api.training.requestStop, { sessionId }),
    },
    {
      name: "the machine sent its end",
      status: "completed",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        accepted(w, "/api/machine/training/end", {
          sessionId,
          failed: false,
          reason: "programme_complete",
        }),
    },
    {
      name: "the machine sent a failed end",
      status: "failed",
      after: (w: MachineWorld, sessionId: Id<"sessions">) =>
        accepted(w, "/api/machine/training/end", {
          sessionId,
          failed: true,
          reason: "drive_fault",
        }),
    },
    {
      name: "the server heard nothing of it for SESSION_SILENCE_MS",
      status: "interrupted",
      after: async (w: MachineWorld) => {
        vi.setSystemTime(NOW + 2 * SECOND + SESSION_SILENCE_MS + SECOND);
        expect(
          await w.t.mutation(internal.training.interruptSilentSessions, {
            cursor: null,
          }),
        ).toEqual({ marked: 1, done: true });
      },
    },
  ])(
    "refuses a repeated confirmation once $name: 400 session_not_pending",
    async ({ status, after }) => {
      const w = await machineWorld();
      const sessionId = await started(w);
      vi.setSystemTime(NOW + 10 * SECOND);
      await after(w, sessionId);
      expect((await rowOf(w, sessionId))?.status).toBe(status);
      const before = await everything(w);

      vi.setSystemTime(Date.now() + MINUTE);
      expect(await confirm(w, { sessionId })).toEqual(notPending(status));
      expect(await everything(w)).toEqual(before);
    },
  );

  it("refuses a launch cancelled before its start, as before", async () => {
    const w = await machineWorld();
    const sessionId = await launch(w);
    vi.setSystemTime(NOW + SECOND);
    await w.admin.mutation(api.training.requestStop, { sessionId });
    const before = await everything(w);

    vi.setSystemTime(NOW + 2 * SECOND);
    expect(await confirm(w, { sessionId })).toEqual(notPending("failed"));
    expect(await everything(w)).toEqual(before);
  });

  it("refuses a session started at the machine, active and silent", async () => {
    const w = await machineWorld();
    const { sessionId } = await accepted(w, "/api/machine/training/local", {
      localRef: "record-1",
      kind: "manual",
      startedAt: NOW,
      operatorName: "Synthetic Operator",
    });
    const before = await everything(w);

    vi.setSystemTime(NOW + MINUTE);
    expect(await confirm(w, { sessionId })).toEqual(notPending("active"));
    expect(await everything(w)).toEqual(before);
  });

  it("refuses the session of another machine of the organisation: 400 session_not_found", async () => {
    const w = await machineWorld();
    const sessionId = await started(w);
    const before = await everything(w);

    vi.setSystemTime(NOW + MINUTE);
    expect(await confirm(w, { sessionId }, w.otherKey)).toEqual({
      status: 400,
      body: { error: "session_not_found", message: "Session not found" },
    });
    expect(await everything(w)).toEqual(before);
  });

  it("refuses, by the internal mutation, the session to a machine of the same or another organisation", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    vi.setSystemTime(NOW + SECOND);
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({ armed: true });
    const before = await everything(w);

    vi.setSystemTime(NOW + MINUTE);
    for (const machineId of [w.otherMachine, w.orgBMachine]) {
      expect(
        await refusalOf(
          w.t.mutation(internal.training.markTrainingStarted, {
            machineId,
            sessionId,
          }),
        ),
      ).toEqual({ code: "session_not_found", message: "Session not found" });
    }
    expect(await everything(w)).toEqual(before);
  });

  it("answers a launch refused at arming the same 409 again", async () => {
    const w = await machineWorld();
    const sessionId = await launch(w);
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 170 }));
    const refused = {
      status: 409,
      body: {
        error: "hr_max_changed_before_start",
        message: ARMING_REFUSALS.hr_max_changed_before_start,
      },
    };

    vi.setSystemTime(NOW + SECOND);
    expect(await confirm(w, { sessionId })).toEqual(refused);
    const before = await everything(w);
    expect((await rowOf(w, sessionId))?.status).toBe("failed");

    vi.setSystemTime(NOW + MINUTE);
    expect(await confirm(w, { sessionId })).toEqual(refused);
    expect(await everything(w)).toEqual(before);
  });
});
