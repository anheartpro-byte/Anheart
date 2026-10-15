/// <reference types="vite/client" />
/**
 * ANH-133: the versioned contract of the machine HTTP routes.
 *
 * - EX-1: the version, the header and the error codes come from the shared file
 *   `contracts/machine-api.json` (that the value is READ from it, and not a
 *   copy that happens to agree, is proven in `contractSource.test.ts`);
 * - EX-3: a request without a served major is answered 426 on every route
 *   but the one that carries the stop request, after the key and before
 *   anything is read or written;
 * - the stop request's route answers whatever the contract, with the key;
 * - EX-4 (server half): the poll answer names the server's contract version;
 * - EX-5: every refusal is `{error: <stable code>, message: <words>}`;
 * - EX-6: each heartbeat stores what the machine announced.
 *
 * ANH-166 adds two codes to the list: `rate_limited` (429) and
 * `payload_too_large` (413), each reached here by a route that answers it.
 */
import { ConvexError } from "convex/values";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import sharedText from "../contracts/machine-api.json?raw";
import { api, internal } from "./_generated/api";
import http from "./http";
import {
  CONTRACT_HEADER,
  CONTRACT_VERSION,
  MACHINE_ERROR_CODES,
  SUPPORTED_MAJORS,
  contractMajor,
  machineError,
  refusalOf,
  softwareVersionOf,
} from "./lib/contract";
import type { Id } from "./_generated/dataModel";
import { machineRoutes } from "./machineAuth.fixtures";
import { modules, NOW, seedMachineWorld } from "./test.setup";

const shared = JSON.parse(sharedText) as {
  contract_version: string;
  header: string;
  server_version_field: string;
  contract_exempt_routes: Record<string, string>;
  error_codes: Record<string, { statuses: number[]; meaning: string }>;
};

/** The routes that answer whatever the contract, as the shared file lists them. */
const exempt = Object.keys(shared.contract_exempt_routes);
/** Every other machine route: a request without a served major stops at 426. */
const enforced = machineRoutes.filter(
  ([method, path]) => !exempt.includes(`${method} ${path}`),
);

const world = () => seedMachineWorld(modules);
type MachineWorld = Awaited<ReturnType<typeof world>>;

/** A request with this machine's key and exactly the contract header given. */
function call(
  w: MachineWorld,
  method: string,
  path: string,
  contract: string | null,
  body?: unknown,
) {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${w.machineKey}`,
    "Content-Type": "application/json",
  };
  if (contract !== null) headers[CONTRACT_HEADER] = contract;
  return w.t.fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

const post = (w: MachineWorld, path: string, body?: unknown) =>
  call(w, "POST", path, CONTRACT_VERSION, body);
const get = (w: MachineWorld, path: string) =>
  call(w, "GET", path, CONTRACT_VERSION);

/**
 * A session of `machineId` for the patient, as `launchAutoSession` queues it:
 * launched by the patient, who holds the launch right on the first machine,
 * dated now (a launch waits five minutes at most, ANH-144).
 */
async function seedSession(
  w: MachineWorld,
  machineId: MachineWorld["machine"],
  status: "pending" | "active",
  startedAt: number = Date.now(),
) {
  return await w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      organizationId: w.organizationId,
      machineId,
      userId: w.patient,
      startedById: w.patient,
      status,
      startedAt,
      channels: ["ECG"],
      kind: "auto" as const,
      origin: "remote" as const,
      profileId: "p1",
      subjectHrMax: 180,
      subjectAge: 36,
    }),
  );
}

/** Confirm, as the first machine, the start of a launch the world changed after it was made. */
async function startAfter(
  w: MachineWorld,
  change: (sessionId: Id<"sessions">) => Promise<unknown>,
  startedAt?: number,
) {
  const sessionId = await seedSession(w, w.machine, "pending", startedAt);
  await change(sessionId);
  return post(w, "/api/machine/training/start", { sessionId });
}

describe("ANH-133 EX-1 one contract, defined in contracts/machine-api.json", () => {
  it("serves the version, header and error codes of the shared file", () => {
    expect(CONTRACT_VERSION).toBe(shared.contract_version);
    expect(CONTRACT_VERSION).toBe("1.6");
    expect(CONTRACT_HEADER).toBe(shared.header);
    expect(MACHINE_ERROR_CODES).toEqual(Object.keys(shared.error_codes));
    expect(MACHINE_ERROR_CODES.length).toBeGreaterThan(0);
  });

  it("is a major.minor version whose major is the one served", () => {
    expect(CONTRACT_VERSION).toMatch(/^\d+\.\d+$/);
    expect(SUPPORTED_MAJORS).toEqual([contractMajor(CONTRACT_VERSION)]);
  });

  it.each([
    ["1.0", "1"],
    ["1.12", "1"],
    ["12.0", "12"],
    ["0.9", "0"],
  ])("reads the major of %s", (version, major) => {
    expect(contractMajor(version)).toBe(major);
  });

  it.each([
    null,
    "",
    "1",
    "1.",
    ".0",
    "1.0.0",
    "v1.0",
    "01.0",
    "1.00",
    "1,0",
    "-1.0",
    "1.0 ",
    "12345.0",
  ])("reads no major from %j", (version) => {
    expect(contractMajor(version)).toBeNull();
  });
});

describe("ANH-133 EX-3 a request without a served major is refused with 426", () => {
  it.each(enforced)(
    "refuses %s %s without the contract header, and names what is served",
    async (method, path) => {
      const w = await world();
      const response = await call(w, method, path, null);
      expect(response.status).toBe(426);
      const payload = (await response.json()) as Record<string, unknown>;
      expect(payload.error).toBe("contract_unsupported");
      expect(payload.supported).toEqual(["1"]);
      expect(typeof payload.message).toBe("string");
    },
  );

  it.each(enforced)(
    "refuses %s %s from a machine speaking another major",
    async (method, path) => {
      const w = await world();
      const response = await call(w, method, path, "2.0");
      expect(response.status).toBe(426);
      expect(((await response.json()) as { error: string }).error).toBe(
        "contract_unsupported",
      );
    },
  );

  it.each(["1", "one", "1.0.0", "0.9", "v1.0"])(
    "refuses the unreadable or unserved version %j",
    async (contract) => {
      const w = await world();
      const response = await call(
        w,
        "POST",
        "/api/machine/heartbeat",
        contract,
      );
      expect(response.status).toBe(426);
    },
  );

  it("accepts a newer minor of the served major", async () => {
    const w = await world();
    const beat = await call(w, "POST", "/api/machine/heartbeat", "1.7", {});
    const poll = await call(w, "GET", "/api/machine/training/poll", "1.7");
    expect(beat.status).toBe(200);
    expect(await beat.json()).toMatchObject({
      success: true,
      server_contract_version: CONTRACT_VERSION,
    });
    expect(poll.status).toBe(200);
  });

  it("writes nothing for a refused request", async () => {
    const w = await world();
    const pending = await seedSession(w, w.machine, "pending");
    const before = await w.t.run((ctx) => ctx.db.get(w.machine));

    const beat = await call(w, "POST", "/api/machine/heartbeat", "2.0", {
      software_version: "pi-9.9.9",
    });
    const start = await call(w, "POST", "/api/machine/training/start", null, {
      sessionId: pending,
    });

    expect(beat.status).toBe(426);
    expect(start.status).toBe(426);
    const after = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(w.machine),
      session: await ctx.db.get(pending),
      beats: await ctx.db.query("machine_heartbeats").collect(),
    }));
    expect(after.machine).toEqual(before);
    expect(after.session?.status).toBe("pending");
    expect(after.beats).toEqual([]);
  });

  it("hands no pending launch to a machine of another major", async () => {
    const w = await world();
    await seedSession(w, w.machine, "pending");
    const response = await call(w, "GET", "/api/machine/training/poll", "2.0");
    expect(response.status).toBe(426);
    expect(await response.json()).not.toHaveProperty("session");
  });

  it("checks the key before the contract", async () => {
    const w = await world();
    const response = await w.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: { [CONTRACT_HEADER]: "2.0" },
    });
    expect(response.status).toBe(401);
    expect(await response.json()).toEqual({
      error: "unauthorized",
      message: "Missing Authorization header",
    });
  });
});

describe("ANH-133 the stop request gets through whatever the contract", () => {
  const STATUS = "/api/machine/training/status";

  /** An active session of the first machine that the dashboard asked to stop. */
  async function seedStopRequested(w: MachineWorld) {
    const sessionId = await seedSession(w, w.machine, "active");
    await w.t.run((ctx) => ctx.db.patch(sessionId, { stopRequestedAt: NOW }));
    return sessionId;
  }

  it("is the only route exempt from the contract, and the shared file says so", () => {
    expect(exempt).toEqual([`GET ${STATUS}`]);
    expect(enforced).toHaveLength(machineRoutes.length - 1);
    // The list these suites iterate is every route the backend registers: a
    // route added without the gate cannot stay out of them.
    expect(
      machineRoutes.map(([method, path]) => `${method} ${path}`).sort(),
    ).toEqual(
      http
        .getRoutes()
        .filter(([path])=>path.startsWith("/api/machine/"))
        .map(([path, method]) => `${method} ${path}`)
        .sort(),
    );
    expect(
      machineRoutes.map(([method, path]) => `${method} ${path}`),
    ).toContain(`GET ${STATUS}`);
  });

  it.each([null, "2.0", "0.9", "one", "1.0.0", CONTRACT_VERSION])(
    "hands the stop request to a machine announcing the contract %j",
    async (contract) => {
      const w = await world();
      const sessionId = await seedStopRequested(w);
      const response = await call(
        w,
        "GET",
        `${STATUS}?sessionId=${sessionId}`,
        contract,
      );
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({
        status: "active",
        active: true,
        stopRequested: true,
        [shared.server_version_field]: CONTRACT_VERSION,
      });
    },
  );

  it("reads only: the session and its machine are as they were", async () => {
    const w = await world();
    const sessionId = await seedStopRequested(w);
    const before = await w.t.run(async (ctx) => ({
      session: await ctx.db.get(sessionId),
      machine: await ctx.db.get(w.machine),
    }));
    await call(w, "GET", `${STATUS}?sessionId=${sessionId}`, "2.0");
    const after = await w.t.run(async (ctx) => ({
      session: await ctx.db.get(sessionId),
      machine: await ctx.db.get(w.machine),
      beats: await ctx.db.query("machine_heartbeats").collect(),
    }));
    expect(after.session).toEqual(before.session);
    expect(after.machine).toEqual(before.machine);
    expect(after.beats).toEqual([]);
  });

  it.each([
    { label: "no key", authorization: undefined },
    {
      label: "an unknown key",
      authorization: `Bearer anh1.${"ab".repeat(16)}.${"cd".repeat(32)}`,
    },
  ])("still requires the machine key ($label)", async ({ authorization }) => {
    const w = await world();
    const sessionId = await seedStopRequested(w);
    const response = await w.t.fetch(`${STATUS}?sessionId=${sessionId}`, {
      method: "GET",
      headers: authorization ? { Authorization: authorization } : {},
    });
    expect(response.status).toBe(401);
    expect(((await response.json()) as { error: string }).error).toBe(
      "unauthorized",
    );
  });

  it("still answers only for a session of the machine that asks", async () => {
    const w = await world();
    const foreign = await seedSession(w, w.otherMachine, "active");
    await w.t.run((ctx) => ctx.db.patch(foreign, { stopRequestedAt: NOW }));
    const response = await call(
      w,
      "GET",
      `${STATUS}?sessionId=${foreign}`,
      "2.0",
    );
    expect(response.status).toBe(404);
    expect(await response.json()).toEqual({
      error: "session_not_found",
      message: "Session not found",
    });
    const missing = await call(w, "GET", STATUS, null);
    expect(missing.status).toBe(400);
    expect(((await missing.json()) as { error: string }).error).toBe(
      "invalid_request",
    );
  });
});

describe("ANH-133 EX-4 the poll answer names the server's contract", () => {
  it("carries server_contract_version with no launch waiting", async () => {
    const w = await world();
    const response = await get(w, "/api/machine/training/poll");
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      session: null,
      [shared.server_version_field]: CONTRACT_VERSION,
    });
  });

  it("carries server_contract_version next to a waiting launch", async () => {
    const w = await world();
    const pending = await seedSession(w, w.machine, "pending");
    const response = await get(w, "/api/machine/training/poll");
    const payload = (await response.json()) as {
      session: { sessionId: string } | null;
      server_contract_version: string;
    };
    expect(payload.session?.sessionId).toBe(pending);
    expect(payload.server_contract_version).toBe(CONTRACT_VERSION);
  });
});

describe("ANH-133 EX-5 every refusal is {error: <stable code>, message}", () => {
  const point = {
    t: NOW,
    elapsedS: 1,
    phase: "hold",
    motorRpm: 1000,
    outputRpm: 20,
    setpointMotorRpm: 1000,
    gLoad: 1.1,
    safetyAction: "none",
  };

  /** Each refusal a machine route can answer, with the code it must carry. */
  const refusals: Array<{
    name: string;
    status: number;
    code: string;
    run: (w: MachineWorld) => Promise<Response>;
  }> = [
    {
      name: "profiles: body that is not an object",
      status: 400,
      code: "invalid_request",
      run: (w) => post(w, "/api/machine/profiles", [1, 2]),
    },
    {
      name: "profiles: a profile that is not an object",
      status: 400,
      code: "invalid_request",
      run: (w) =>
        post(w, "/api/machine/profiles", { storeRev: 1, profiles: [3] }),
    },
    {
      name: "profiles: a malformed profile",
      status: 400,
      code: "invalid_request",
      run: (w) =>
        post(w, "/api/machine/profiles", {
          storeRev: 1,
          profiles: [{ profileId: "p", name: "n" }],
        }),
    },
    {
      name: "training/start: no session id",
      status: 400,
      code: "invalid_request",
      run: (w) => post(w, "/api/machine/training/start", {}),
    },
    {
      name: "training/start: a session of another machine",
      status: 400,
      code: "session_not_found",
      run: async (w) =>
        post(w, "/api/machine/training/start", {
          sessionId: await seedSession(w, w.otherMachine, "pending"),
        }),
    },
    {
      name: "training/start: a session that is no longer pending",
      status: 400,
      code: "session_not_pending",
      run: async (w) =>
        post(w, "/api/machine/training/start", {
          sessionId: await seedSession(w, w.machine, "active"),
        }),
    },
    // ANH-144: the launch is checked again when the machine arms it.
    {
      name: "training/start: a launch that waited longer than PENDING_SESSION_TTL_S",
      status: 409,
      code: "expired_before_start",
      run: (w) => startAfter(w, async () => {}, Date.now() - 301_000),
    },
    {
      name: "training/start: a machine that left the session's organisation",
      status: 409,
      code: "organization_changed_before_start",
      run: (w) =>
        startAfter(w, () =>
          w.t.run(async (ctx) => {
            const elsewhere = await ctx.db.insert("organizations", {
              name: "Elsewhere",
              slug: "elsewhere",
              createdAt: NOW,
              settings: { requirePrescription: false, language: "fr" },
            });
            await ctx.db.patch(w.machine, { organizationId: elsewhere });
          }),
        ),
    },
    {
      name: "training/start: a launch right withdrawn while it waited",
      status: 409,
      code: "right_revoked_before_start",
      run: (w) =>
        startAfter(w, () =>
          w.t.run(async (ctx) => {
            for (const row of await ctx.db
              .query("machine_user_permissions")
              .collect()) {
              await ctx.db.delete(row._id);
            }
          }),
        ),
    },
    ...(["rider_not_fit", "fitness_expired", "consent_missing"] as const).map(
      (code) => ({
        name: `training/start: ${code} while pending`,
        status: 409,
        code,
        run: (w: MachineWorld) =>
          startAfter(w, () =>
            w.t.run(async (ctx) => {
              if (code === "consent_missing") {
                for (const consent of await ctx.db.query("consents").collect()) {
                  await ctx.db.patch(consent._id, { withdrawnAt: Date.now() });
                }
              } else {
                const assessment = await ctx.db.query("fitness_assessments").first();
                if (assessment === null) throw new Error("Missing test fitness");
                await ctx.db.patch(
                  assessment._id,
                  code === "rider_not_fit"
                    ? { validatedAt: undefined }
                    : { validUntil: Date.now() },
                );
              }
            }),
          ),
      }),
    ),
    {
      name: "training/start: a birth year that now gives a minor",
      status: 409,
      code: "age_refused_before_start",
      run: (w) =>
        startAfter(w, () =>
          w.t.run((ctx) =>
            ctx.db.patch(w.patient, {
              birthYear: new Date().getUTCFullYear() - 15,
            }),
          ),
        ),
    },
    {
      name: "training/start: a max heart rate changed while it waited",
      status: 409,
      code: "hr_max_changed_before_start",
      run: (w) =>
        startAfter(w, () =>
          w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 175 })),
        ),
    },
    {
      name: "training/start: a rider already running on another machine",
      status: 409,
      code: "rider_busy",
      run: (w) =>
        startAfter(w, () => seedSession(w, w.otherMachine, "active")),
    },
    {
      name: "training/start: an identifier that is not one",
      status: 400,
      code: "request_failed",
      run: (w) =>
        post(w, "/api/machine/training/start", { sessionId: "not-an-id" }),
    },
    {
      name: "training/local: missing fields",
      status: 400,
      code: "invalid_request",
      run: (w) => post(w, "/api/machine/training/local", { kind: "auto" }),
    },
    {
      name: "training/end: missing reason",
      status: 400,
      code: "invalid_request",
      run: (w) => post(w, "/api/machine/training/end", { sessionId: "x" }),
    },
    {
      name: "training/end: a session of another machine",
      status: 400,
      code: "session_not_found",
      run: async (w) =>
        post(w, "/api/machine/training/end", {
          sessionId: await seedSession(w, w.otherMachine, "active"),
          failed: false,
          reason: "programme_complete",
        }),
    },
    {
      name: "training/status: no session id",
      status: 400,
      code: "invalid_request",
      run: (w) => get(w, "/api/machine/training/status"),
    },
    {
      name: "training/status: a session of another machine",
      status: 404,
      code: "session_not_found",
      run: async (w) =>
        get(
          w,
          `/api/machine/training/status?sessionId=${await seedSession(w, w.otherMachine, "active")}`,
        ),
    },
    {
      name: "training/telemetry: no points",
      status: 400,
      code: "invalid_request",
      run: (w) =>
        post(w, "/api/machine/training/telemetry", { sessionId: "x" }),
    },
    {
      name: "training/telemetry: a malformed point",
      status: 400,
      code: "invalid_request",
      run: (w) =>
        post(w, "/api/machine/training/telemetry", {
          sessionId: "x",
          points: [{ ...point, gLoad: "heavy" }],
        }),
    },
    {
      name: "training/telemetry: more than 600 points",
      status: 413,
      code: "payload_too_large",
      run: (w) =>
        post(w, "/api/machine/training/telemetry", {
          sessionId: "x",
          points: Array.from({ length: 601 }, () => point),
        }),
    },
    {
      name: "training/telemetry: a session of another machine",
      status: 400,
      code: "session_not_found",
      run: async (w) =>
        post(w, "/api/machine/training/telemetry", {
          sessionId: await seedSession(w, w.otherMachine, "active"),
          points: [point],
        }),
    },
    {
      name: "training/events: no events",
      status: 400,
      code: "invalid_request",
      run: (w) => post(w, "/api/machine/training/events", { sessionId: "x" }),
    },
    {
      name: "training/events: a malformed event",
      status: 400,
      code: "invalid_request",
      run: (w) =>
        post(w, "/api/machine/training/events", {
          sessionId: "x",
          events: [{ seq: 0, t: NOW, kind: "phase", detail: 12, actor: "system" }],
        }),
    },
    {
      name: "training/events: more than 200 events",
      status: 413,
      code: "payload_too_large",
      run: (w) =>
        post(w, "/api/machine/training/events", {
          sessionId: "x",
          events: Array.from({ length: 201 }, (_, seq) => ({
            seq,
            t: NOW,
            kind: "phase",
            detail: "hold",
            actor: "system",
          })),
        }),
    },
    {
      name: "training/events: a session of another machine",
      status: 400,
      code: "session_not_found",
      run: async (w) =>
        post(w, "/api/machine/training/events", {
          sessionId: await seedSession(w, w.otherMachine, "active"),
          events: [
            { seq: 0, t: NOW, kind: "phase", detail: "hold", actor: "system" },
          ],
        }),
    },
    {
      name: "heartbeat: a second one within 5 s",
      status: 429,
      code: "rate_limited",
      run: async (w) => {
        await post(w, "/api/machine/heartbeat", {});
        return post(w, "/api/machine/heartbeat", {});
      },
    },
    {
      name: "any route: an unknown key",
      status: 401,
      code: "unauthorized",
      run: (w) =>
        w.t.fetch("/api/machine/training/poll", {
          method: "GET",
          headers: {
            Authorization: `Bearer anh1.${"ab".repeat(16)}.${"cd".repeat(32)}`,
            [CONTRACT_HEADER]: CONTRACT_VERSION,
          },
        }),
    },
    {
      name: "any route: no contract header",
      status: 426,
      code: "contract_unsupported",
      run: (w) => call(w, "GET", "/api/machine/roster", null),
    },
  ];

  it.each(refusals)("$name -> $status $code", async ({ run, status, code }) => {
    const w = await world();
    const response = await run(w);
    expect(response.status).toBe(status);
    const payload = (await response.json()) as Record<string, unknown>;
    expect(payload.error).toBe(code);
    expect(typeof payload.message).toBe("string");
    expect(payload.message).not.toBe("");
    // The code is one of the shared list, at one of the statuses it declares.
    expect(shared.error_codes[code]?.statuses).toContain(status);
  });

  it("exercises every code of the shared list but those no route can reach", () => {
    const exercised = new Set(refusals.map((refusal) => refusal.code));
    // A deleted machine is refused its key (401) before its start is read:
    // `machine_deleted_before_start` is the end code the sweep writes, and a
    // start answers it only to a machine deleted while the start is handled.
    expect(MACHINE_ERROR_CODES.filter((code) => !exercised.has(code))).toEqual([
      "machine_not_found",
      "machine_deleted_before_start",
    ]);
  });

  it("registering a local session for a machine that is gone carries machine_not_found", async () => {
    const w = await world();
    await w.t.run((ctx) => ctx.db.delete(w.machine));
    const refused = await w.t
      .mutation(internal.training.registerLocalSession, {
        machineId: w.machine,
        localRef: "local-ref-gone",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Synthetic Operator",
      })
      .then(
        () => null,
        (error: unknown) => error,
      );
    expect(refusalOf(refused)).toEqual({
      code: "machine_not_found",
      message: "Machine not found",
    });
  });

  it("keeps the code and words of an error raised with one", () => {
    expect(
      refusalOf(machineError("session_not_pending", "Session is not pending")),
    ).toEqual({
      code: "session_not_pending",
      message: "Session is not pending",
    });
  });

  it.each([
    { thrown: new Error("boom"), message: "boom" },
    { thrown: "boom", message: "Unknown error" },
    { thrown: new ConvexError("plain words"), message: "plain words" },
    { thrown: new ConvexError(null), message: "null" },
    {
      thrown: new ConvexError({ code: "not_in_the_list", message: "m" }),
      message: '{"code":"not_in_the_list","message":"m"}',
    },
    {
      thrown: new ConvexError({ code: "session_not_found", message: 3 }),
      message: '{"code":"session_not_found","message":3}',
    },
  ])(
    "answers request_failed for an error without a known code ($message)",
    ({ thrown, message }) => {
      expect(refusalOf(thrown)).toEqual({ code: "request_failed", message });
    },
  );
});

describe("ANH-133 EX-6 each heartbeat stores what the machine announced", () => {
  // The server's clock is set, and moved by a heartbeat period between two
  // heartbeats: a machine takes one every 5 s (ANH-166).
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it("stores the software version, the contract version and when they were seen", async () => {
    const w = await world();
    const started = Date.now();
    const response = await call(w, "POST", "/api/machine/heartbeat", "1.3", {
      software_version: "pi-0.4.2",
      contract_version: "1.3",
      medical_parameters_version: null,
      config_hash: null,
    });
    expect(response.status).toBe(200);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(machine?.softwareVersion).toBe("pi-0.4.2");
    expect(machine?.contractVersion).toBe("1.3");
    expect(machine?.lastVersionSeenAt).toBeGreaterThanOrEqual(started);
    expect(machine?.lastVersionSeenAt).toBe(machine?.lastHeartbeat);
  });

  it("shows them on the machine page's query", async () => {
    const w = await world();
    await post(w, "/api/machine/heartbeat", { software_version: "pi-0.4.2" });
    const shown = await w.admin.query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(shown?.softwareVersion).toBe("pi-0.4.2");
    expect(shown?.contractVersion).toBe(CONTRACT_VERSION);
    expect(typeof shown?.lastVersionSeenAt).toBe("number");
  });

  it("shows nothing before the first heartbeat", async () => {
    const w = await world();
    const shown = await w.admin.query(api.machines.getMachine, {
      machineId: w.machine,
    });
    expect(shown).not.toBeNull();
    expect(shown?.softwareVersion).toBeUndefined();
    expect(shown?.contractVersion).toBeUndefined();
    expect(shown?.lastVersionSeenAt).toBeUndefined();
  });

  it.each([
    { label: "absent", body: {} },
    { label: "not a string", body: { software_version: 42 } },
    { label: "empty", body: { software_version: "" } },
    { label: "markup", body: { software_version: "<b>pi</b>" } },
    { label: "too long", body: { software_version: "p".repeat(65) } },
  ])(
    "clears a software version the next heartbeat does not state ($label)",
    async ({ body }) => {
      const w = await world();
      await post(w, "/api/machine/heartbeat", { software_version: "pi-0.4.2" });
      vi.setSystemTime(NOW + 10_000);
      const response = await post(w, "/api/machine/heartbeat", body);
      expect(response.status).toBe(200);
      const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
      expect(machine?.softwareVersion).toBeUndefined();
      expect(machine?.contractVersion).toBe(CONTRACT_VERSION);
    },
  );

  it("leaves the versions alone when a heartbeat is recorded without a contract", async () => {
    const w = await world();
    await post(w, "/api/machine/heartbeat", { software_version: "pi-0.4.2" });
    const before = await w.t.run((ctx) => ctx.db.get(w.machine));
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
    });
    const after = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect(after?.softwareVersion).toBe("pi-0.4.2");
    expect(after?.contractVersion).toBe(before?.contractVersion);
    expect(after?.lastVersionSeenAt).toBe(before?.lastVersionSeenAt);
  });

  it.each(["pi-0.4.2", "pi-0.0.0-dev", "pi-1.2.3+build.7", "p".repeat(64)])(
    "accepts the software version %s",
    (version) => {
      expect(softwareVersionOf(version)).toBe(version);
    },
  );

  it.each([
    undefined,
    null,
    3,
    "",
    " pi-1",
    "pi 1",
    "-pi",
    "pi\n1",
    "p".repeat(65),
  ])("accepts no software version from %j", (raw) => {
    expect(softwareVersionOf(raw)).toBeUndefined();
  });
});
