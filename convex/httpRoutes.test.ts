/// <reference types="vite/client" />
/**
 * ANH-132: the machine HTTP routes in `convex/http.ts`.
 *
 * Authentication refusals (missing header, malformed/unknown key, deleted or
 * disabled machine, regenerated key) across all fourteen routes are already
 * proven by `machineAuth.test.ts`; this suite does not repeat them. It covers
 * the per-route contract: malformed bodies, idempotency, machine binding, and
 * the pending-session filtering the Pi depends on.
 */
import { describe, expect, it } from "vitest";
import { modules, NOW, seedMachineWorld } from "./test.setup";
import type { Id } from "./_generated/dataModel";

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

async function seedSession(
  w: MachineWorld,
  machineId: Id<"machines">,
  status: "pending" | "active" | "completed" | "failed",
  kind: "recording" | "auto",
) {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      machineId,
      userId: w.patient,
      status,
      startedAt: NOW,
      channels: ["ECG"],
      kind,
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
    { path: "/api/machine/profiles", body: { storeRev: "nope" } },
  ])("rejects a malformed payload on $path", async ({ path, body }) => {
    const w = await world();
    const response = await send(w, w.machineKey, "POST", path, body);
    expect(response.status).toBe(400);
    const payload = (await response.json()) as { error?: string };
    expect(typeof payload.error).toBe("string");
  });
});

describe("ANH-132 /api/machine/training/poll", () => {
  it("returns only a pending auto session of this machine", async () => {
    const w = await world();
    const mine = await seedPendingAuto(w, w.machine);
    await seedPendingAuto(w, w.otherMachine); // another machine's auto
    await seedSession(w, w.machine, "pending", "recording"); // not an auto

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
    const payload = (await response.json()) as { error?: string };
    expect(payload.error).toMatch(/not pending/);
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
    const payload = (await response.json()) as { stored: number };
    expect(payload.stored).toBe(1);
  });

  it("refuses a session that belongs to another machine (400)", async () => {
    const w = await world();
    const foreign = await seedSession(w, w.otherMachine, "active", "auto");
    const response = await send(w, w.machineKey, "POST", "/api/machine/training/telemetry", {
      sessionId: foreign,
      points: [point],
    });
    expect(response.status).toBe(400);
    const payload = (await response.json()) as { error?: string };
    expect(payload.error).toMatch(/Session not found/);
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
// Legacy ECG-only routes (used by raspberry-pi/src/convex_client.py).
// ---------------------------------------------------------------------------

describe("ANH-132 legacy /api/machine/session/poll", () => {
  it("returns a pending recording session, never an auto one", async () => {
    const w = await world();
    // The auto session is older, so a filter that just took the first pending
    // row would return it: the recording-only filter is what keeps it out.
    await seedPendingAuto(w, w.machine);
    const recording = await seedSession(w, w.machine, "pending", "recording");
    const response = await send(w, w.machineKey, "GET", "/api/machine/session/poll");
    expect(response.status).toBe(200);
    const payload = (await response.json()) as { session: { id: string } | null };
    expect(payload.session?.id).toBe(recording);
  });
});

describe("ANH-132 legacy /api/machine/session lifecycle (own session)", () => {
  it("starts, reports status for, and ends this machine's recording session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "pending", "recording");

    const start = await send(w, w.machineKey, "POST", "/api/machine/session/start", {
      sessionId,
    });
    expect(start.status).toBe(200);
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe("active");

    const status = await send(
      w,
      w.machineKey,
      "GET",
      `/api/machine/session/status?sessionId=${sessionId}`,
    );
    expect(status.status).toBe(200);
    expect(((await status.json()) as { active: boolean }).active).toBe(true);

    const end = await send(w, w.machineKey, "POST", "/api/machine/session/end", {
      sessionId,
    });
    expect(end.status).toBe(200);
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe("completed");
  });

  it("returns 404 for an unknown session status and 400 for a missing id", async () => {
    const w = await world();
    const missing = await send(w, w.machineKey, "GET", "/api/machine/session/status");
    expect(missing.status).toBe(400);
    const sessionId = await seedSession(w, w.machine, "pending", "recording");
    await w.t.run((ctx) => ctx.db.delete(sessionId));
    const unknown = await send(
      w,
      w.machineKey,
      "GET",
      `/api/machine/session/status?sessionId=${sessionId}`,
    );
    expect(unknown.status).toBe(404);
  });
});

describe("ANH-132 legacy /api/machine/data", () => {
  const batch = (sessionId: string, timestamp: number) => ({
    sessionId,
    timestamp,
    sampleRate: 250,
    samples: [{ channel: "ECG", values: [1, 2, 3], unit: "mV" }],
  });

  it("stores a batch for this machine's active session", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "recording");
    const response = await send(
      w,
      w.machineKey,
      "POST",
      "/api/machine/data",
      batch(sessionId, Date.now()),
    );
    expect(response.status).toBe(200);
  });

  it("refuses a batch for this machine's session that is not active (400)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "pending", "recording");
    const response = await send(
      w,
      w.machineKey,
      "POST",
      "/api/machine/data",
      batch(sessionId, Date.now()),
    );
    expect(response.status).toBe(400);
    const payload = (await response.json()) as { error?: string };
    expect(payload.error).toMatch(/not active/);
    expect(await w.t.run((ctx) => ctx.db.query("ecg_data").collect())).toEqual([]);
  });

  it.each([
    { label: "invalid JSON", raw: "not json" },
  ])("refuses $label with 400", async ({ raw }) => {
    const w = await world();
    const response = await sendRaw(w, w.machineKey, "POST", "/api/machine/data", raw);
    expect(response.status).toBe(400);
  });

  it("refuses a timestamp too far in the future or too far in the past (400)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "recording");
    const future = await send(
      w,
      w.machineKey,
      "POST",
      "/api/machine/data",
      batch(sessionId, Date.now() + 120000),
    );
    const past = await send(
      w,
      w.machineKey,
      "POST",
      "/api/machine/data",
      batch(sessionId, Date.now() - 600000),
    );
    expect(future.status).toBe(400);
    expect(past.status).toBe(400);
  });

  it("refuses a missing timestamp (400)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "recording");
    const response = await send(w, w.machineKey, "POST", "/api/machine/data", {
      sessionId,
      samples: [{ channel: "ECG", values: [1] }],
    });
    expect(response.status).toBe(400);
  });
});

// ---------------------------------------------------------------------------
// ANH-177: a session must belong to the authenticated machine.
// ---------------------------------------------------------------------------

/**
 * Every route that reads or changes a session designated by its identifier
 * (eight routes, two of them with two body forms), called by the first machine
 * for `sessionId`, with the status it answers for a session it does not know.
 */
const sessionRoutes: Array<{
  route: string;
  unknownStatus: 400 | 404;
  call: (w: MachineWorld, sessionId: string) => Promise<Response>;
}> = [
  {
    route: "session/start",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/session/start", { sessionId }),
  },
  {
    route: "session/end",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/session/end", { sessionId }),
  },
  {
    route: "session/end (failed)",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/session/end", {
        sessionId,
        failed: true,
        reason: "synthetic reason",
      }),
  },
  {
    route: "session/status",
    unknownStatus: 404,
    call: (w, sessionId) =>
      send(
        w,
        w.machineKey,
        "GET",
        `/api/machine/session/status?sessionId=${sessionId}`,
      ),
  },
  {
    route: "data",
    unknownStatus: 400,
    call: (w, sessionId) =>
      send(w, w.machineKey, "POST", "/api/machine/data", {
        sessionId,
        timestamp: Date.now(),
        sampleRate: 250,
        samples: [{ channel: "ECG", values: [1, 2, 3], unit: "mV" }],
      }),
  },
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
];

const sessionStates = ["pending", "active", "completed", "failed"] as const;
const sessionKinds = ["recording", "auto"] as const;

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
      expect(JSON.parse(onForeign.body)).toEqual({ error: "Session not found" });
      // The session, its machine and the stored measurements are unchanged.
      const after = await w.t.run(async (ctx) => ({
        session: await ctx.db.get(foreign),
        machine: await ctx.db.get(w.otherMachine),
        ecg: await ctx.db.query("ecg_data").collect(),
        telemetry: await ctx.db.query("training_telemetry").collect(),
      }));
      expect(after.session).toEqual(before.session);
      expect(after.machine).toEqual(before.machine);
      expect(after.ecg).toEqual([]);
      expect(after.telemetry).toEqual([]);
    },
  );
});

describe("ANH-177 legacy session routes for the owning machine", () => {
  it("end with failed marks this machine's session failed (200)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "recording");
    const response = await send(w, w.machineKey, "POST", "/api/machine/session/end", {
      sessionId,
      failed: true,
      reason: "synthetic reason",
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ success: true });
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.status).toBe("failed");
    expect(session?.notes).toContain("synthetic reason");
    expect((await w.t.run((ctx) => ctx.db.get(w.machine)))?.status).toBe("offline");
  });

  it("start refuses a session that is not pending (400)", async () => {
    const w = await world();
    const sessionId = await seedSession(w, w.machine, "active", "recording");
    const response = await send(w, w.machineKey, "POST", "/api/machine/session/start", {
      sessionId,
    });
    expect(response.status).toBe(400);
    expect(((await response.json()) as { error: string }).error).toMatch(/not pending/);
  });
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
