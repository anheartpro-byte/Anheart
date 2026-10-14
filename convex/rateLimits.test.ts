/// <reference types="vite/client" />
/**
 * ANH-166: rate limits per machine and per account, and the largest body of a
 * machine route.
 *
 * - EX-1: each limit of the shared contract (`contracts/machine-api.json`,
 *   `rate_limits`) is met on its route, per machine, with a 429
 *   `{error: "rate_limited", message, retryAfterMs}` and `Retry-After`; the
 *   stop request never is. Per account: `launchAutoSession` five a minute,
 *   every public write sixty a minute (a stop excepted), refused with a
 *   `ConvexError` coded `rate_limited`.
 * - EX-2: a body over its route's size is refused with 413 before its JSON
 *   is read, and so is a batch of more items than its route takes; the
 *   largest batch a machine may send still fits.
 * - Acceptance: 1000 heartbeats in 10 s from one machine are limited, with
 *   no error on the Convex side.
 *
 * The server's clock is the fake `Date`, moved by hand: a bucket fills with
 * time, and nothing else here waits.
 */
import { ConvexError, type Value } from "convex/values";
import { getFunctionName, makeFunctionReference } from "convex/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import sharedText from "../contracts/machine-api.json?raw";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { MATRIX } from "./authorization.matrix";
import {
  MAX_EVENTS_BYTES,
  MAX_HEARTBEAT_BYTES,
  MAX_TELEMETRY_BYTES,
  rateLimiter,
} from "./lib/rateLimits";
import { machineHeaders } from "./machineAuth.fixtures";
import { mockClerkInvitations } from "./patientInvitation.fixtures";
import {
  as,
  modules,
  NOW,
  seedMachineWorld,
  seedWorld,
  type World,
} from "./test.setup";

const shared = JSON.parse(sharedText) as {
  rate_limits: {
    routes: Record<
      string,
      { rate: number; period_ms: number; capacity: number }
    >;
  };
  max_body_bytes: { routes: Record<string, number> };
  error_codes: Record<string, { statuses: number[] }>;
};

type MachineWorld = Awaited<ReturnType<typeof seedMachineWorld>>;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

/** Let `ms` pass on the server's clock. */
const later = (ms: number) => vi.setSystemTime(Date.now() + ms);

function request(
  w: MachineWorld,
  key: string,
  method: string,
  path: string,
  body?: string,
  headers: Record<string, string> = {},
) {
  return w.t.fetch(path, {
    method,
    headers: { ...machineHeaders(key), ...headers },
    body,
  });
}

/** A local session of the first machine, the target of the batch routes. */
async function localSession(w: MachineWorld): Promise<Id<"sessions">> {
  const response = await request(
    w,
    w.machineKey,
    "POST",
    "/api/machine/training/local",
    JSON.stringify({
      localRef: `ref-${Date.now()}`,
      kind: "manual",
      startedAt: NOW,
      operatorName: "Synthetic Operator",
    }),
  );
  expect(response.status).toBe(200);
  return ((await response.json()) as { sessionId: Id<"sessions"> }).sessionId;
}

const POINT = {
  t: NOW,
  elapsedS: 0,
  phase: "hold",
  motorRpm: 0,
  outputRpm: 0,
  setpointMotorRpm: 0,
  gLoad: 0,
  safetyAction: "none",
};

/** A request each limited route takes, built for a world. */
const ROUTES: Record<
  string,
  (w: MachineWorld) => Promise<{ method: string; path: string; body?: string }>
> = {
  "POST /api/machine/heartbeat": async () => ({
    method: "POST",
    path: "/api/machine/heartbeat",
    body: "{}",
  }),
  "GET /api/machine/training/poll": async () => ({
    method: "GET",
    path: "/api/machine/training/poll",
  }),
  "POST /api/machine/training/telemetry": async (w) => ({
    method: "POST",
    path: "/api/machine/training/telemetry",
    body: JSON.stringify({ sessionId: await localSession(w), points: [POINT] }),
  }),
  "POST /api/machine/training/events": async (w) => ({
    method: "POST",
    path: "/api/machine/training/events",
    body: JSON.stringify({
      sessionId: await localSession(w),
      events: [
        { seq: 0, t: NOW, kind: "phase", detail: "hold", actor: "system" },
      ],
    }),
  }),
  // Metadata the route refuses (400): the token is taken before the body is read.
  "POST /api/machine/records/upload-url": async () => ({
    method: "POST",
    path: "/api/machine/records/upload-url",
    body: "{}",
  }),
};

async function rateLimited(response: Response) {
  expect(response.status).toBe(429);
  const payload = (await response.json()) as Record<string, unknown>;
  expect(payload.error).toBe("rate_limited");
  expect(typeof payload.message).toBe("string");
  expect(Number.isInteger(payload.retryAfterMs)).toBe(true);
  const retryAfterMs = payload.retryAfterMs as number;
  expect(response.headers.get("Retry-After")).toBe(
    String(Math.ceil(retryAfterMs / 1000)),
  );
  return retryAfterMs;
}

describe("ANH-166 EX-1 the limits of the shared contract, per machine", () => {
  it("names exactly the routes it limits, among the machine routes", () => {
    expect(Object.keys(shared.rate_limits.routes).sort()).toEqual(
      Object.keys(ROUTES).sort(),
    );
    expect(shared.rate_limits.routes).not.toHaveProperty(
      "GET /api/machine/training/status",
    );
    expect(shared.error_codes.rate_limited.statuses).toEqual([429]);
    expect(shared.error_codes.payload_too_large.statuses).toEqual([413]);
  });

  it.each(Object.entries(shared.rate_limits.routes))(
    "%s: takes its capacity, then answers 429 with the wait, then takes one again",
    async (route, { rate, period_ms, capacity }) => {
      const w = await seedMachineWorld(modules);
      const send = async () => {
        const { method, path, body } = await ROUTES[route](w);
        return request(w, w.machineKey, method, path, body);
      };

      for (let n = 0; n < capacity; n++) {
        expect((await send()).status).not.toBe(429);
      }
      const refused = await send();
      const retryAfterMs = await rateLimited(refused);
      expect(retryAfterMs).toBeGreaterThan(0);
      expect(retryAfterMs).toBeLessThanOrEqual(Math.ceil(period_ms / rate));

      // Asked again just before the wait is over: still refused.
      later(retryAfterMs - 1);
      await rateLimited(await send());
      later(1);
      expect((await send()).status).not.toBe(429);
      // Another machine was never held by this one.
      const { method, path, body } = await ROUTES[route](w);
      expect(
        (await request(w, w.otherKey, method, path, body)).status,
      ).not.toBe(429);
    },
  );

  it("writes nothing for a heartbeat it refuses", async () => {
    const w = await seedMachineWorld(modules);
    const beat = () =>
      request(w, w.machineKey, "POST", "/api/machine/heartbeat", "{}");
    expect((await beat()).status).toBe(200);
    const before = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(w.machine),
      beats: await ctx.db.query("machine_heartbeats").collect(),
    }));

    later(1_000);
    await rateLimited(await beat());

    const after = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(w.machine),
      beats: await ctx.db.query("machine_heartbeats").collect(),
    }));
    expect(after).toEqual(before);
  });

  it("refuses an over-limit request before reading its body, however large", async () => {
    const w = await seedMachineWorld(modules);
    await request(w, w.machineKey, "POST", "/api/machine/heartbeat", "{}");

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/heartbeat",
      "x".repeat(MAX_HEARTBEAT_BYTES + 1),
    );

    await rateLimited(response);
  });

  it("never limits the stop request", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await localSession(w);

    const statuses: number[] = [];
    for (let n = 0; n < 50; n++) {
      const response = await request(
        w,
        w.machineKey,
        "GET",
        `/api/machine/training/status?sessionId=${sessionId}`,
      );
      statuses.push(response.status);
    }

    expect(new Set(statuses)).toEqual(new Set([200]));
  });
});

describe("ANH-166 acceptance: 1000 heartbeats in 10 s from one machine", () => {
  it("are limited, with no error on the Convex side", async () => {
    const w = await seedMachineWorld(modules);
    const statuses: number[] = [];

    // One every 10 ms, from 0 to 9.99 s.
    for (let n = 0; n < 1000; n++) {
      vi.setSystemTime(NOW + n * 10);
      const response = await request(
        w,
        w.machineKey,
        "POST",
        "/api/machine/heartbeat",
        "{}",
      );
      statuses.push(response.status);
    }

    // One at 0 s, one at 5 s: the bucket holds one heartbeat and gains one every 5 s.
    const accepted = statuses.flatMap((status, n) =>
      status === 200 ? [n * 10] : [],
    );
    expect(accepted).toEqual([0, 5_000]);
    expect(statuses.filter((status) => status === 429)).toHaveLength(998);
    const beats = await w.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(beats).toHaveLength(2);
    // The limiter's own state: one bucket, for this machine and this route.
    const state = await w.t.run((ctx) =>
      rateLimiter.getValue(ctx, "machineHeartbeat", { key: w.machine }),
    );
    expect(state.ts).toBe(NOW + 5_000);
  });
});

describe("ANH-166 EX-2 the largest body of a machine route", () => {
  it("reads its sizes from the shared contract", () => {
    expect(MAX_HEARTBEAT_BYTES).toBe(16_384);
    expect(MAX_TELEMETRY_BYTES).toBe(
      shared.max_body_bytes.routes["POST /api/machine/training/telemetry"],
    );
    expect(MAX_EVENTS_BYTES).toBe(
      shared.max_body_bytes.routes["POST /api/machine/training/events"],
    );
  });

  async function tooLarge(response: Response, sizeWords: RegExp) {
    expect(response.status).toBe(413);
    const payload = (await response.json()) as Record<string, unknown>;
    expect(payload.error).toBe("payload_too_large");
    expect(payload.message).toMatch(sizeWords);
  }

  it("takes a heartbeat of exactly 16 KiB, and refuses one byte more without recording it", async () => {
    const w = await seedMachineWorld(modules);
    // `{"pad":"…"}`: 10 bytes around the padding.
    const exactly = JSON.stringify({
      pad: "p".repeat(MAX_HEARTBEAT_BYTES - 10),
    });
    expect(new TextEncoder().encode(exactly).byteLength).toBe(
      MAX_HEARTBEAT_BYTES,
    );
    const over = JSON.stringify({ pad: "p".repeat(MAX_HEARTBEAT_BYTES - 9) });

    expect(
      (
        await request(
          w,
          w.machineKey,
          "POST",
          "/api/machine/heartbeat",
          exactly,
        )
      ).status,
    ).toBe(200);
    later(10_000);
    await tooLarge(
      await request(w, w.machineKey, "POST", "/api/machine/heartbeat", over),
      /16384 bytes/,
    );

    const beats = await w.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(beats).toHaveLength(1);
  });

  it("counts bytes, not characters: two-byte characters over the size are refused", async () => {
    const w = await seedMachineWorld(modules);
    const body = JSON.stringify({ pad: "é".repeat(MAX_HEARTBEAT_BYTES / 2) });
    expect(body.length).toBeLessThan(MAX_HEARTBEAT_BYTES);

    await tooLarge(
      await request(w, w.machineKey, "POST", "/api/machine/heartbeat", body),
      /16384 bytes/,
    );
  });

  it("refuses on the declared length alone, before a byte of the body is read", async () => {
    const w = await seedMachineWorld(modules);

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/heartbeat",
      "{}",
      { "Content-Length": String(MAX_HEARTBEAT_BYTES + 1) },
    );

    await tooLarge(response, /16384 bytes/);
  });

  it("does not trust a declared length smaller than the body", async () => {
    const w = await seedMachineWorld(modules);

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/heartbeat",
      JSON.stringify({ pad: "p".repeat(MAX_HEARTBEAT_BYTES) }),
      { "Content-Length": "2" },
    );

    await tooLarge(response, /16384 bytes/);
  });

  it.each([
    [
      "telemetry",
      "/api/machine/training/telemetry",
      MAX_TELEMETRY_BYTES,
      "points",
    ],
    ["events", "/api/machine/training/events", MAX_EVENTS_BYTES, "events"],
  ] as const)(
    "refuses a %s body over its size, and stores nothing of it",
    async (_name, path, max, field) => {
      const w = await seedMachineWorld(modules);
      const sessionId = await localSession(w);
      const head = JSON.stringify({ sessionId, [field]: [] });
      // Valid JSON, one byte over: the list stays empty, the padding is a field.
      const body = `${head.slice(0, -1)},"pad":"${"p".repeat(max - head.length - 8)}"}`;
      expect(new TextEncoder().encode(body).byteLength).toBe(max + 1);

      await tooLarge(
        await request(w, w.machineKey, "POST", path, body),
        new RegExp(`${max} bytes`),
      );
      const stored = await w.t.run(async (ctx) => ({
        points: await ctx.db.query("training_telemetry").collect(),
        events: await ctx.db.query("training_events").collect(),
      }));
      expect(stored).toEqual({ points: [], events: [] });
    },
  );

  it("refuses 601 points before reading one, and stores none", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await localSession(w);

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/training/telemetry",
      // The 601st is not even a point: the count is refused before any is read.
      JSON.stringify({
        sessionId,
        points: [
          ...Array.from({ length: 600 }, (_, n) => ({ ...POINT, t: NOW + n })),
          7,
        ],
      }),
    );

    await tooLarge(response, /600 points/);
    expect(
      await w.t.run((ctx) => ctx.db.query("training_telemetry").collect()),
    ).toEqual([]);
  });

  it("still takes the largest batch of events a machine may send", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await localSession(w);
    // 200 events of 2000 characters, each written `é` as the console's
    // JSON writes a character that is not ASCII: 6 bytes each.
    const detail = "\\u00e9".repeat(2000);
    const event = (seq: number) =>
      `{"seq": ${999_800 + seq}, "t": ${NOW + seq}, "kind": "${"k".repeat(64)}", "detail": "${detail}", "actor": "${"a".repeat(64)}"}`;
    const body = `{"sessionId": "${sessionId}", "events": [${Array.from(
      { length: 200 },
      (_, seq) => event(seq),
    ).join(", ")}]}`;
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(
      MAX_EVENTS_BYTES,
    );

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/training/events",
      body,
    );

    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      stored: 200,
      duplicates: 0,
      rejected: 0,
    });
  });

  it("still takes 600 points written at their longest", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await localSession(w);
    // Numbers of 17 significant digits and a sign, words of 32 characters.
    const long = -1234.5678901234567;
    const words = "w".repeat(32);
    const points = Array.from({ length: 600 }, (_, n) => ({
      t: NOW + n,
      elapsedS: long,
      phase: words,
      bpm: long,
      motorRpm: long,
      outputRpm: long,
      setpointMotorRpm: long,
      gLoad: long,
      safetyAction: words,
    }));
    const body = JSON.stringify({ sessionId, points }, null, 1);
    expect(new TextEncoder().encode(body).byteLength).toBeLessThanOrEqual(
      MAX_TELEMETRY_BYTES,
    );

    const response = await request(
      w,
      w.machineKey,
      "POST",
      "/api/machine/training/telemetry",
      body,
    );

    expect(response.status).toBe(200);
  });
});

// ---------------------------------------------------------------------------
// Per account
// ---------------------------------------------------------------------------

/** The `ConvexError` of an account over its limit. */
async function refusedForRate(call: Promise<unknown>) {
  const error = await call.then(
    () => null,
    (thrown: unknown) => thrown,
  );
  expect(error).toBeInstanceOf(ConvexError);
  const data = (error as ConvexError<Record<string, Value>>).data;
  expect(data.code).toBe("rate_limited");
  expect(typeof data.message).toBe("string");
  expect(Number.isInteger(data.retryAfterMs)).toBe(true);
  expect(data.retryAfterMs as number).toBeGreaterThan(0);
  return data.retryAfterMs as number;
}

/** Take every token left of an account's limit, as calls in a row would. */
async function exhaust(
  w: World,
  name: "userWrite" | "launchAutoSession",
  subject: string,
) {
  let taken = 0;
  while (
    (await w.t.run((ctx) => rateLimiter.limit(ctx, name, { key: subject }))).ok
  ) {
    taken += 1;
  }
  expect(taken).toBeLessThanOrEqual(name === "userWrite" ? 60 : 5);
}

describe("ANH-166 EX-1 per account", () => {
  it("takes sixty writes a minute from one account, then refuses the next with rate_limited", async () => {
    const w = await seedWorld(modules);
    const me = as(w.t, "stranger");

    for (let n = 0; n < 60; n++) {
      await me.mutation(api.users.updateUserProfile, { firstName: `n${n}` });
    }
    const retryAfterMs = await refusedForRate(
      me.mutation(api.users.updateUserProfile, { firstName: "61st" }),
    );

    // Nothing of the refused write was written; another account writes on.
    const row = await w.t.run((ctx) => ctx.db.get(w.stranger));
    expect(row?.firstName).toBe("n59");
    await as(w.t, "patient").mutation(api.users.updateUserProfile, {
      firstName: "other",
    });
    // A minute's sixtieth later, one more.
    expect(retryAfterMs).toBeLessThanOrEqual(1_000);
    later(retryAfterMs);
    await me.mutation(api.users.updateUserProfile, { firstName: "again" });
  });

  it("counts a write only when it happens: refused calls give their token back", async () => {
    const w = await seedWorld(modules);
    const me = as(w.t, "stranger");

    for (let n = 0; n < 80; n++) {
      // A role change is an admin's: refused, and rolled back with its token.
      await expect(
        me.mutation(api.users.updateUserRole, {
          userId: w.patient,
          role: "admin",
        }),
      ).rejects.toThrow(/Unauthorized/);
    }

    await me.mutation(api.users.updateUserProfile, { firstName: "still" });
  });

  it("takes five launches a minute from one account, then refuses the sixth", async () => {
    const w = await seedWorld(modules);
    const manager = as(w.t, "manager");
    const launch = () =>
      manager.mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
        userId: w.patient,
      });

    for (let n = 0; n < 5; n++) {
      const sessionId = await launch();
      // Cancelled at once, so that the machine is free for the next one.
      await manager.mutation(api.training.requestStop, { sessionId });
    }
    const retryAfterMs = await refusedForRate(launch());

    // One launch every twelve seconds after that.
    expect(retryAfterMs).toBeLessThanOrEqual(12_000);
    const pending = await w.t.run((ctx) =>
      ctx.db
        .query("sessions")
        .filter((q) => q.eq(q.field("status"), "pending"))
        .collect(),
    );
    expect(pending).toEqual([]);
  });

  it("refuses a launch once the account's writes are spent, whatever its launches", async () => {
    const w = await seedWorld(modules);
    await exhaust(w, "userWrite", "manager");

    await refusedForRate(
      as(w.t, "manager").mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
        userId: w.patient,
      }),
    );
  });

  it("never refuses a stop, even to an account whose writes are spent", async () => {
    const w = await seedWorld(modules);
    const manager = as(w.t, "manager");
    const sessionId = await manager.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      profileId: w.profileId,
      userId: w.patient,
    });
    await exhaust(w, "userWrite", "manager");
    await exhaust(w, "launchAutoSession", "manager");

    await manager.mutation(api.training.requestStop, { sessionId });

    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.status).toBe("failed");
  });
});

describe("ANH-166 EX-1 every public write of an account is limited, the stop excepted", () => {
  beforeEach(mockClerkInvitations);

  const writes = MATRIX.filter(
    (entry) => entry.kind === "mutation" || entry.kind === "action",
  );

  it("covers every public mutation and action of the matrix", () => {
    expect(writes.map((entry) => entry.id)).toContain(
      "training.launchAutoSession",
    );
    expect(writes.length).toBeGreaterThanOrEqual(26);
  });

  for (const entry of writes) {
    // A cell that succeeds when the account has writes left; for a function
    // no cell succeeds with (a role is Clerk's once Organizations is on), any
    // signed-in caller: the limit is taken before anything else is checked.
    const signedIn = entry.cases.filter((c) => c.actor !== "anonymous");
    const cell =
      signedIn.find((c) => c.expect.outcome === "success" && !c.knownDefect) ??
      signedIn[0];
    if (!cell) continue;
    const exempt = entry.id === "training.requestStop";
    it(`${entry.id}: ${exempt ? "still runs" : "is refused"} once ${cell.actor}'s writes are spent`, async () => {
      const w = await seedWorld(modules);
      const scope = cell.scope ?? "none";
      const args = await entry.build(w, cell.actor, scope);
      const claims = entry.claims?.(w, cell.actor, scope) ?? null;
      const caller = as(w.t, cell.actor, claims);
      await exhaust(w, "userWrite", cell.actor);

      const name = getFunctionName(entry.ref);
      const call =
        entry.kind === "action"
          ? caller.action(
              makeFunctionReference<"action", Record<string, unknown>, unknown>(
                name,
              ),
              args,
            )
          : caller.mutation(
              makeFunctionReference<
                "mutation",
                Record<string, unknown>,
                unknown
              >(name),
              args,
            );

      if (exempt) {
        await call;
      } else {
        await refusedForRate(call);
      }
    });
  }
});
