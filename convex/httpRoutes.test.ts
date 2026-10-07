/// <reference types="vite/client" />
/**
 * ANH-132: the machine HTTP routes in `convex/http.ts`.
 *
 * Authentication refusals (missing header, malformed/unknown key, deleted or
 * disabled machine, regenerated key) across all ten routes are already
 * proven by `machineAuth.test.ts`; this suite does not repeat them. It covers
 * the per-route contract: malformed bodies, idempotency, machine binding, and
 * the pending-session filtering the Pi depends on.
 */
import { describe, expect, it } from "vitest";
import http from "./http";
import { modules, NOW, seedMachineWorld } from "./test.setup";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";

const world = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof world>>;

function send(
  w: MachineWorld,
  key: string,
  method: "GET" | "POST",
  path: string,
  body?: unknown,
) {
  return w.t.fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function sendRaw(
  w: MachineWorld,
  key: string,
  method: "GET" | "POST",
  path: string,
  raw: string,
) {
  return w.t.fetch(path, {
    method,
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
      [CONTRACT_HEADER]: CONTRACT_VERSION,
    },
    body: raw,
  });
}

async function seedPendingAuto(w: MachineWorld, machineId: Id<"machines">) {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      machineId,
      userId: w.patient,
      status: "pending" as const,
      startedAt: NOW,
      channels: ["ECG"],
      kind: "auto" as const,
      origin: "remote" as const,
      profileId: "p1",
      profileName: "Programme p1",
      subjectHrMax: 180,
      subjectAge: 36,
      operatorName: "Synthetic Operator",
    }),
  );
}

/**
 * A session of this machine. "legacy" is a session of the retired ECG
 * recording mode: a row without `kind`, as that mode wrote them.
 */
async function seedSession(
  w: MachineWorld,
  machineId: Id<"machines">,
  status: "pending" | "active" | "completed" | "failed",
  kind: "legacy" | "auto",
) {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      machineId,
      userId: w.patient,
      status,
      startedAt: NOW,
      channels: ["ECG"],
      kind: kind === "legacy" ? undefined : kind,
    }),
  );
}

const unknownButWellFormedKey = `anh1.${"ab".repeat(16)}.${"cd".repeat(32)}`;

describe("ANH-132 machine route authentication edges", () => {
  it("rejects an unknown but well-formed key with 401", async () => {
    const w = await world();
    const response = await send(
      w,
      unknownButWellFormedKey,
      "POST",
      "/api/machine/heartbeat",
    );
    expect(response.status).toBe(401);
  });

  it("records the live panel state from a heartbeat body", async () => {
    const w = await world();
    const response = await send(w, w.machineKey, "POST", "/api/machine/heartbeat", {
      programsEnabled: true,
      live: {
        runMode: "seance",
        phase: "hold",
        bpm: 140,
        motorRpm: 1000,
        outputRpm: 20,
        setpointMotorRpm: 1000,
        gLoad: 1.1,
        safetyAction: "none",
      },
    });
    expect(response.status).toBe(200);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.live?.phase).toBe("hold");
  });
});

describe("ANH-132 malformed bodies return 400 {error}", () => {
  it.each([
    { path: "/api/machine/profiles", body: "not json at all" },
  ])("rejects an unparseable body on $path", async ({ path, body }) => {
    const w = await world();
    const response = await sendRaw(w, w.machineKey, "POST", path, body);
    expect(response.status).toBe(400);
    const payload = (await response.json()) as { error?: string };
    expect(typeof payload.error).toBe("string");
  });

  it.each([
    { path: "/api/machine/training/start", body: {} },
    { path: "/api/machine/training/end", body: { sessionId: "x" } },
    { path: "/api/machine/training/telemetry", body: { sessionId: "x" } },
    { path: "/api/machine/training/events", body: { sessionId: "x" } },
    { path: "/api/machine/profiles", body: { storeRev: "nope" } },
  ])("rejects a malformed payload on $path", async ({ path, body }) => {
    const w = await world();
    const response = await send(w, w.machineKey, "POST", path, body);
    expect(response.status).toBe(400);
    const payload = (await response.json()) as { error?: string };
    expect(typeof payload.error).toBe("string");
  });
});

// ---------------------------------------------------------------------------
// ANH-195 EX-3: a body that is JSON but not an object is refused.
// ---------------------------------------------------------------------------

/**
 * Every route of the router that takes a body, read from the router itself: a
 * route added later is held to the same answer without being listed here.
 */
const bodyRoutes = http
  .getRoutes()
  .filter(([, method]) => method === "POST")
  .map(([path]) => path)
  .sort();

/** What JSON can hold at its top level besides an object. */
const notAnObject = [
  { kind: "null", raw: "null" },
  { kind: "a number", raw: "42" },
  { kind: "a string", raw: '"heartbeat"' },
  { kind: "a boolean", raw: "true" },
  { kind: "an empty array", raw: "[]" },
  { kind: "an array of objects", raw: '[{"sessionId":"x","points":[]}]' },
];

/** Everything a machine route can write for the first machine. */
async function written(w: MachineWorld) {
  return await w.t.run(async (ctx) => ({
    machine: await ctx.db.get(w.machine),
    heartbeats: await ctx.db.query("machine_heartbeats").collect(),
    profiles: await ctx.db.query("machine_profiles").collect(),
    sessions: await ctx.db.query("sessions").collect(),
    telemetry: await ctx.db.query("training_telemetry").collect(),
  }));
}

describe("ANH-195 EX-3 a JSON body that is not an object is refused with 400", () => {
  // Not an exact list: a route added to the router joins the table below by
  // itself. This only holds that the table was read, and is not empty.
  it("covers at least the six routes that took a body when this was written", () => {
    expect(bodyRoutes).toEqual(
      expect.arrayContaining([
        "/api/machine/heartbeat",
        "/api/machine/profiles",
        "/api/machine/training/end",
        "/api/machine/training/local",
        "/api/machine/training/start",
        "/api/machine/training/telemetry",
      ]),
    );
  });

  const cases = bodyRoutes.flatMap((path) =>
    notAnObject.map((body) => ({ path, ...body })),
  );

  it.each(cases)("$path refuses $kind", async ({ path, raw }) => {
    const w = await world();
    const before = await written(w);

    const response = await sendRaw(w, w.machineKey, "POST", path, raw);
    const payload: unknown = await response.json();
    const after = await written(w);

    expect(response.status).toBe(400);
    expect(payload).toEqual({
      error: "invalid_request",
      message: expect.any(String),
    });
    expect(after).toEqual(before);
  });

  it("still takes a heartbeat without a body, and one whose body is an object", async () => {
    const w = await world();

    const empty = await sendRaw(w, w.machineKey, "POST", "/api/machine/heartbeat", "");
    const object = await sendRaw(w, w.machineKey, "POST", "/api/machine/heartbeat", "{}");
    const beats = await w.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );

    expect(empty.status).toBe(200);
    expect(object.status).toBe(200);
    expect(beats).toHaveLength(2);
  });
});

describe("ANH-132 /api/machine/training/poll", () => {
  it("returns only a pending auto session of this machine", async () => {
    const w = await world();
    const mine = await seedPendingAuto(w, w.machine);
    await seedPendingAuto(w, w.otherMachine); // another machine's auto
    await seedSession(w, w.machine, "pending", "legacy"); // not an auto

    const response = await send(w, w.machineKey, "GET", "/api/machine/training/poll");
    expect(response.status).toBe(200);
    const payload = (await response.json()) as {
      session: { sessionId: string } | null;
    };
    expect(payload.session?.sessionId).toBe(mine);
  });

  it("returns null when this machine has no pending auto session", async () => {
    const w = await world();
    await seedPendingAuto(w, w.otherMachine);
    const response = await send(w, w.machineKey, "GET", "/api/machine/training/poll");
    const payload = (await response.json()) as { session: unknown };
    expect(payload.session).toBeNull();
  });
});

describe("ANH-132 /api/machine/training/start", () => {
  it("starts a pending auto session", async () => {
    const w = await world();
    const sessionId = await seedPendingAuto(w, w.machine);
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/start", {
      sessionId,
    });
    expect(response.status).toBe(200);
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.status).toBe("active");
  });

  it("refuses a session that is no longer pending (400)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/start", {
      sessionId,
    });
    expect(response.status).toBe(400);
    const payload = (await response.json()) as {
      error?: string;
      message?: string;
    };
    expect(payload.error).toBe("session_not_pending");
    expect(payload.message).toMatch(/not pending/);
  });
});

describe("ANH-132 /api/machine/training/local is idempotent", () => {
  it("returns the same session for a repeated localRef", async () => {
    const w = await world();
    const body = {
      localRef: "local-ref-1",
      kind: "manual" as const,
      startedAt: NOW,
      operatorName: "Synthetic Operator",
    };
    const first = await send(w, w.machineKey, "POST", "/api/machine/training/local", body);
    const second = await send(w, w.machineKey, "POST", "/api/machine/training/local", body);
    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    const a = (await first.json()) as { sessionId: string };
    const b = (await second.json()) as { sessionId: string };
    expect(a.sessionId).toBe(b.sessionId);
    const sessions = await w.t.run((ctx) => ctx.db.query("sessions").collect());
    expect(sessions.filter((s) => s.localRef === "local-ref-1")).toHaveLength(1);
  });
});

describe("ANH-132 /api/machine/training/end is idempotent", () => {
  it("accepts a repeated end and keeps the session finished", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    const body = { sessionId, failed: false, reason: "programme_complete" };
    const first = await send(w, w.machineKey, "POST", "/api/machine/training/end", body);
    const second = await send(w, w.machineKey, "POST", "/api/machine/training/end", body);
    expect(first.status).toBe(200);
    expect(second.status).toBe(200);
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.status).toBe("completed");
  });
});

describe("ANH-132 /api/machine/training/telemetry binding", () => {
  const point = {
    t: NOW,
    elapsedS: 1,
    phase: "hold",
    bpm: 140,
    motorRpm: 1000,
    outputRpm: 20,
    setpointMotorRpm: 1000,
    gLoad: 1.1,
    safetyAction: "none",
  };

  it("stores telemetry for this machine's session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/telemetry", {
      sessionId,
      points: [point],
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ stored: 1, duplicates: 0, rejected: 0 });
  });

  it("refuses a session that belongs to another machine (400)", async () => {
    const w = await world();
    const foreign = await seedSession(w, w.otherMachine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/telemetry", {
      sessionId: foreign,
      points: [point],
    });
    expect(response.status).toBe(400);
    const payload = (await response.json()) as {
      error?: string;
      message?: string;
    };
    expect(payload.error).toBe("session_not_found");
    expect(payload.message).toBe("Session not found");
  });
});

describe("ANH-129 /api/machine/training/events binding", () => {
  const event = { seq: 0, t: NOW, kind: "phase", detail: "hold", actor: "system" };

  it("stores the events of this machine's session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/events", {
      sessionId,
      events: [event],
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ stored: 1, duplicates: 0, rejected: 0 });
  });

  it("refuses a session that belongs to another machine (400)", async () => {
    const w = await world();
    const foreign = await seedSession(w, w.otherMachine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/events", {
      sessionId: foreign,
      events: [event],
    });
    expect(response.status).toBe(400);
    expect(await response.json()).toEqual({
      error: "session_not_found",
      message: "Session not found",
    });
  });
});

describe("ANH-132 /api/machine/training/status", () => {
  it("reports the status of this machine's session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    const response = await send(
      w,
      w.machineKey,
      "GET",
      `/api/machine/training/status?sessionId=${sessionId}`,
    );
    expect(response.status).toBe(200);
    const payload = (await response.json()) as { active: boolean };
    expect(payload.active).toBe(true);
  });

  it("returns 404 for an unknown session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "auto");
    await w.t.run((ctx) => ctx.db.delete(sessionId));
    const response = await send(
      w,
      w.machineKey,
      "GET",
      `/api/machine/training/status?sessionId=${sessionId}`,
    );
    expect(response.status).toBe(404);
  });
});

describe("ANH-132 /api/machine/roster", () => {
  it("lists the riders holding a launch right on this machine", async () => {
    const w = await world();
    const response = await send(w, w.machineKey, "GET", "/api/machine/roster");
    expect(response.status).toBe(200);
    const payload = (await response.json()) as {
      riders: Array<{ userId: string }>;
    };
    expect(payload.riders.map((r) => r.userId)).toContain(w.patient);
  });
});

describe("ANH-132 /api/machine/profiles", () => {
  it("replaces the machine's profiles and reports the count", async () => {
    const w = await world();
    const response = await send(w, w.machineKey, "POST", "/api/machine/profiles", {
      storeRev: 2,
      programsEnabled: true,
      profiles: [
        {
          profileId: "p9",
          name: "Programme p9",
          totalDurationS: 900,
          zoneLowBpm: 120,
          zoneHighBpm: 140,
          hardMaxBpm: 160,
          criticalBpm: 180,
          subjectHrMax: 190,
          minRunRpm: 20,
          maxRpm: 1400,
        },
      ],
    });
    expect(response.status).toBe(200);
    const payload = (await response.json()) as { count: number };
    expect(payload.count).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// ANH-177: a session must belong to the authenticated machine.
// ---------------------------------------------------------------------------

/**
 * Every route that reads or changes a session designated by its identifier
 * (five routes, one of them with two body forms), called by the first machine
 * for `sessionId`, with the status it answers for a session it does not know.
 */
const sessionRoutes: Array<{
  route: string;
  unknownStatus: 400 | 404;
  call: (w: MachineWorld, sessionId: string) => Promise<Response>;
}> = [
  {
    route: "training/start",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/training/start", { sessionId }),
  },
  {
    route: "training/end",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/training/end", {
        sessionId,
        failed: false,
        reason: "programme_complete",
      }),
  },
  {
    route: "training/end (failed)",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/training/end", {
        sessionId,
        failed: true,
        reason: "synthetic reason",
      }),
  },
  {
    route: "training/status",
    unknownStatus: 404,
    call: (w, sessionId) =>
      send(
        w,
        w.machineKey,
        "GET",
        `/api/machine/training/status?sessionId=${sessionId}`,
      ),
  },
  {
    route: "training/telemetry",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/training/telemetry", {
        sessionId,
        points: [
          {
            t: NOW,
            elapsedS: 1,
            phase: "hold",
            bpm: 140,
            motorRpm: 1000,
            outputRpm: 20,
            setpointMotorRpm: 1000,
            gLoad: 1.1,
            safetyAction: "none",
          },
        ],
      }),
  },
  {
    route: "training/events",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/training/events", {
        sessionId,
        events: [
          { seq: 0, t: NOW, kind: "phase", detail: "hold", actor: "system" },
        ],
      }),
  },
];

const sessionStates = ["pending", "active", "completed", "failed"] as const;
const sessionKinds = ["legacy", "auto"] as const;

/** Status, headers and body: everything the caller can observe. */
async function fullResponse(response: Response) {
  const headers: Array<[string, string]> = [];
  response.headers.forEach((value, key) => {
    headers.push([key, value]);
  });
  return {
    status: response.status,
    headers: headers.sort(),
    body: await response.text(),
  };
}

describe("ANH-177 a session of another machine is answered like an unknown session", () => {
  const cases = sessionRoutes.flatMap((route) =>
    sessionStates.flatMap((state) =>
      sessionKinds.map((kind) => ({ ...route, state, kind })),
    ),
  );

  it.each(cases)(
    "$route: $state $kind session",
    async ({ call, unknownStatus, state, kind }) => {
      const w = await world();
      const foreign = await seedSession(w, w.otherMachine, state, kind);
      const unknown = await seedSession(w, w.otherMachine, state, kind);
      await w.t.run((ctx) => ctx.db.delete(unknown));
      const before = await w.t.run(async (ctx) => ({
        session: await ctx.db.get(foreign),
        machine: await ctx.db.get(w.otherMachine),
      }));

      const onForeign = await fullResponse(await call(w, foreign));
      const onUnknown = await fullResponse(await call(w, unknown));

      expect(onForeign).toEqual(onUnknown);
      expect(onForeign.status).toBe(unknownStatus);
      // A stable code, the same for both (ANH-133).
      expect(JSON.parse(onForeign.body)).toEqual({
        error: "session_not_found",
        message: "Session not found",
      });
      // The session, its machine and the stored measurements are unchanged.
      const after = await w.t.run(async (ctx) => ({
        session: await ctx.db.get(foreign),
        machine: await ctx.db.get(w.otherMachine),
        ecg: await ctx.db.query("ecg_data").collect(),
        telemetry: await ctx.db.query("training_telemetry").collect(),
        events: await ctx.db.query("training_events").collect(),
      }));
      expect(after.session).toEqual(before.session);
      expect(after.machine).toEqual(before.machine);
      expect(after.ecg).toEqual([]);
      expect(after.telemetry).toEqual([]);
      expect(after.events).toEqual([]);
    },
  );
});

describe("ANH-177 roster is limited to this machine's riders", () => {
  it("excludes a rider who only holds a right on another machine", async () => {
    const w = await world();
    const outsider = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("users", {
        clerkId: "roster-outsider",
        role: "user" as const,
        firstName: "Roster",
        lastName: "Outsider",
        email: "roster-outsider@example.invalid",
        language: "en" as const,
        createdAt: NOW,
      });
      await ctx.db.insert("machine_user_permissions", {
        machineId: w.otherMachine,
        userId: id,
        grantedBy: w.adminId,
        createdAt: NOW,
      });
      return id;
    });
    const response = await send(w, w.machineKey, "GET", "/api/machine/roster");
    expect(response.status).toBe(200);
    const riders = ((await response.json()) as { riders: Array<{ userId: string }> })
      .riders.map((r) => r.userId);
    expect(riders).toContain(w.patient);
    expect(riders).not.toContain(outsider);
  });
});
