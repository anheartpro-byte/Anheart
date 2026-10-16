import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import shared from "../contracts/machine-api.json";
import { api, internal } from "./_generated/api";
import { CONTRACT_HEADER } from "./lib/contract";
import { rateLimiter } from "./lib/rateLimits";
import { machineHeaders } from "./machineAuth.fixtures";
import { modules, NOW, seedMachineWorld } from "./test.setup";

type World = Awaited<ReturnType<typeof seedMachineWorld>>;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

function send(
  w: World,
  path: string,
  body?: unknown,
  headers = machineHeaders(w.machineKey),
) {
  return w.t.fetch(`/api/machine/${path}`, {
    method: body === undefined ? "GET" : "POST",
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

function declaration(localRef: string) {
  return {
    localRef,
    kind: "manual" as const,
    startedAt: NOW,
    operatorName: "Synthetic Operator",
  };
}

async function session(w: World) {
  return w.t.mutation(internal.training.registerLocalSession, {
    machineId: w.machine,
    ...declaration("synthetic-fixture"),
  });
}

const NEVER_LIMITED = [
  "GET /api/machine/training/status",
  "POST /api/machine/training/end",
  "POST /api/machine/training/start",
  "GET /api/machine/records/status",
  "GET /api/machine/roster",
] as const;

describe("ANH-228 refused writes", () => {
  it("the 21st local declaration adds neither a session nor an audit entry", async () => {
    const w = await seedMachineWorld(modules);
    for (let n = 0; n < 20; n++) {
      expect(
        (await send(w, "training/local", declaration(`synthetic-${n}`))).status,
      ).toBe(200);
    }
    const snapshot = () =>
      w.t.run(async (ctx) => ({
        sessions: await ctx.db.query("sessions").collect(),
        audit: await ctx.db.query("audit_log").collect(),
        machine: await ctx.db.get(w.machine),
      }));
    const before = await snapshot();
    expect(before.sessions).toHaveLength(20);
    const refused = await send(
      w,
      "training/local",
      declaration("synthetic-21"),
    );
    expect(refused.status).toBe(429);
    expect(await snapshot()).toEqual(before);
    vi.setSystemTime(NOW + 60_000);
    expect(
      (await send(w, "training/local", declaration("synthetic-21"))).status,
    ).toBe(200);
    expect((await snapshot()).sessions).toHaveLength(21);
  });

  it("a refused profile revision leaves the machine's programs unchanged", async () => {
    const w = await seedMachineWorld(modules);
    const profile = await w.t.run((ctx) =>
      ctx.db
        .query("machine_profiles")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .first(),
    );
    expect(profile).not.toBeNull();
    const revision = {
      storeRev: 2,
      programsEnabled: true,
      profiles: [profile],
    };
    for (let n = 0; n < 5; n++) {
      expect((await send(w, "profiles", revision)).status).toBe(200);
    }
    const snapshot = () =>
      w.t.run(async (ctx) => ({
        programs: await ctx.db.query("machine_profiles").collect(),
        machine: await ctx.db.get(w.machine),
      }));
    const before = await snapshot();
    const refused = await send(w, "profiles", {
      storeRev: 3,
      programsEnabled: false,
      profiles: [],
    });
    expect(refused.status).toBe(429);
    expect(await snapshot()).toEqual(before);
  });

  it.each([
    ["training/local", 20],
    ["profiles", 5],
    ["records/commit", 64],
  ] as const)(
    "%s refuses before reading any JSON body",
    async (path, capacity) => {
      const w = await seedMachineWorld(modules);
      for (let n = 0; n < capacity; n++) {
        expect((await send(w, path, {})).status).toBe(400);
      }
      const read = vi
        .spyOn(Request.prototype, "json")
        .mockRejectedValue(new Error("Body must not be read"));
      expect((await send(w, path, { synthetic: "unread" })).status).toBe(429);
      expect(read).not.toHaveBeenCalled();
    },
  );
});

describe("ANH-228 routes that must never depend on a limiter", () => {
  it("keeps exactly the five exempt routes outside the contract's limits", () => {
    for (const route of NEVER_LIMITED) {
      expect(Object.keys(shared.rate_limits.routes)).not.toContain(route);
    }
  });

  it("still serves all five routes with every machine bucket exhausted", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await session(w);
    const pending = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.otherMachine,
      profileId: w.profileId,
      userId: w.patient,
    });
    const headers = machineHeaders(w.otherKey);
    for (const [route, limit] of Object.entries(shared.rate_limits.routes)) {
      const [method, path] = route.split(" ");
      for (let n = 0; n < limit.capacity; n++) {
        const response = await w.t.fetch(path, {
          method,
          headers,
          body: method === "GET" ? undefined : "{}",
        });
        expect(response.status).not.toBe(429);
      }
      expect(
        (
          await w.t.fetch(path, {
            method,
            headers,
            body: method === "GET" ? undefined : "{}",
          })
        ).status,
      ).toBe(429);
    }
    expect(
      (await send(w, "training/start", { sessionId: pending }, headers)).status,
    ).toBe(200);
    const status = await send(
      w,
      `training/status?sessionId=${pending}`,
      undefined,
      headers,
    );
    expect(status.status).toBe(200);
    expect(await status.json()).toMatchObject({
      active: true,
      stopRequested: false,
    });
    expect(
      (
        await send(
          w,
          "training/end",
          { sessionId: pending, failed: false, reason: "synthetic_stop" },
          headers,
        )
      ).status,
    ).toBe(200);
    const records = await send(
      w,
      `records/status?sessionId=${pending}`,
      undefined,
      headers,
    );
    expect(records.status).toBe(200);
    expect(await records.json()).toMatchObject({ record: "none", files: [] });
    const roster = await send(w, "roster", undefined, headers);
    expect(roster.status).toBe(200);
    expect(await roster.json()).toHaveProperty("riders");
    expect(
      (await send(w, `training/status?sessionId=${sessionId}`)).status,
    ).toBe(200);
  });

  it("serves 100 consecutive stop requests and 100 session ends", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await session(w);
    await w.admin.mutation(api.training.requestStop, { sessionId });
    for (let n = 0; n < 100; n++) {
      const response = await send(w, `training/status?sessionId=${sessionId}`);
      expect(response.status).toBe(200);
      expect(await response.json()).toMatchObject({ stopRequested: true });
    }
    for (let n = 0; n < 100; n++) {
      expect(
        (
          await send(w, "training/end", {
            sessionId,
            failed: false,
            reason: "synthetic_stop",
          })
        ).status,
      ).toBe(200);
    }
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
      "completed",
    );
  });

  it("still delivers the stop and end when the limiter throws", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await session(w);
    await w.admin.mutation(api.training.requestStop, { sessionId });
    vi.spyOn(rateLimiter, "limit").mockRejectedValue(
      new Error("Synthetic limiter failure"),
    );
    await expect(send(w, "heartbeat", {})).rejects.toThrow(
      "Synthetic limiter failure",
    );
    const response = await send(w, `training/status?sessionId=${sessionId}`);
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ stopRequested: true });
    expect(
      (
        await send(w, "training/end", {
          sessionId,
          failed: false,
          reason: "synthetic_stop",
        })
      ).status,
    ).toBe(200);
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
      "completed",
    );
  });
});

describe("ANH-228 EX-2 key, then contract, then bucket", () => {
  const refusals = [
    "invalid",
    "revoked",
    "replaced",
    "major",
    "absent",
  ] as const;
  it.each(refusals)(
    "%s refusals spend no token on any HTTP gate",
    async (refusal) => {
      const w = await seedMachineWorld(modules);
      const replacement = await w.admin.mutation(
        api.machineKeys.createMachineKey,
        {
          machineId: w.machine,
          overlapMinutes: refusal === "replaced" ? 1 : 0,
        },
      );
      if (refusal === "replaced") vi.setSystemTime(NOW + 60_000);
      const valid = machineHeaders(replacement.apiKey);
      const rejected = { ...valid };
      let expected: number;
      if (
        refusal === "invalid" ||
        refusal === "revoked" ||
        refusal === "replaced"
      ) {
        rejected.Authorization = `Bearer ${refusal === "invalid" ? "synthetic-invalid" : w.machineKey}`;
        expected = 401;
      } else {
        if (refusal === "major") rejected[CONTRACT_HEADER] = "2.0";
        else delete rejected[CONTRACT_HEADER];
        expected = 426;
      }
      const sessionId = await session(w);
      await w.t.mutation(internal.training.endTrainingSession, {
        machineId: w.machine,
        sessionId,
        failed: false,
        reason: "synthetic_stop",
      });
      const routes = [
        ["heartbeat", {}, 1],
        ["training/poll", undefined, 1],
        ["training/local", declaration("synthetic-auth-order"), 20],
        ["profiles", { storeRev: 2, programsEnabled: true, profiles: [] }, 5],
        [
          "records/upload-url",
          { sessionId, path: "manifest.json", size: 0, sha256: "a".repeat(64) },
          64,
        ],
      ] as const;
      const limit = vi.spyOn(rateLimiter, "limit");
      for (const [path, body, capacity] of routes) {
        limit.mockClear();
        for (let n = 0; n <= capacity; n++) {
          expect((await send(w, path, body, rejected)).status).toBe(expected);
        }
        expect(limit).not.toHaveBeenCalled();
        for (let n = 0; n < capacity; n++) {
          expect((await send(w, path, body, valid)).status).toBe(200);
        }
        expect((await send(w, path, body, valid)).status).toBe(429);
      }
    },
  );
});
