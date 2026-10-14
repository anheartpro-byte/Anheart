/// <reference types="vite/client" />
/**
 * ANH-216 EX-1: what closes, on the server, an active session whose machine
 * never sends its end (console killed, record unreadable, machine never
 * switched on again), and what never does.
 *
 * - the job of `check-offline-machines` marks `interrupted` (end unknown) an
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
 * Everything runs in memory with `convex-test`, the server's clock faked.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { SESSION_SILENCE_MS } from "../lib/training";
import { machineHeaders } from "./machineAuth.fixtures";
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

/** The job, as the cron runs it at `at` on the server's clock. */
async function jobAt(w: { t: MachineWorld["t"] }, at: number) {
  vi.setSystemTime(at);
  return await w.t.mutation(internal.machines.checkOfflineMachines, {});
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
    expect(before.interruptedSessionCount).toBe(0);
    expect((await sessionOf(w, sessionId))?.status).toBe("active");

    const after = await jobAt(w, NOW + SESSION_SILENCE_MS + SECOND);
    expect(after.interruptedSessionCount).toBe(1);
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
    expect(again.interruptedSessionCount).toBe(0);
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
    const confirmed = NOW + 30 * MINUTE;
    vi.setSystemTime(confirmed);
    await post(w, "/api/machine/training/start", { sessionId });

    await jobAt(w, confirmed + SESSION_SILENCE_MS - SECOND);
    expect((await sessionOf(w, sessionId))?.status).toBe("active");
    await jobAt(w, confirmed + SESSION_SILENCE_MS + SECOND);
    expect((await sessionOf(w, sessionId))?.status).toBe("interrupted");
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

    expect(report.interruptedSessionCount).toBe(0);
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
      expect(report.interruptedSessionCount).toBe(0);
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
    expect(report.interruptedSessionCount).toBe(0);
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
      ).rejects.toThrow(/Session not found/);
      expect((await sessionOf(w, sessionId))?.stopRequestedAt).toBeUndefined();
    },
  );

  it.each(["stranger", "otherManager"] as const)(
    "%s of the same organisation, without access to it, may not stop it",
    async (actor) => {
      const { w, sessionId } = await interruptedWorld();
      await expect(
        as(w.t, actor).mutation(api.training.requestStop, { sessionId }),
      ).rejects.toThrow(/Not authorized to stop this session/);
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
