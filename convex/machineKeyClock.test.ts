import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { v } from "convex/values";
import { api, internal } from "./_generated/api";
import { internalMutation, internalQuery } from "./_generated/server";
import { CONTRACT_HEADER } from "./lib/contract";
import { authenticatedMachine, authenticateMachine } from "./lib/machineAuth";
import {
  authenticateMachineRequest,
  validateMachineAuth,
} from "./lib/machineHttpAuth";
import { KEY_USE_REFRESH_MS } from "./lib/machineKeys";
import { rateLimiter, type MachineLimit } from "./lib/rateLimits";
import {
  machineFixture,
  machineHeaders,
  machineRoutes,
} from "./machineAuth.fixtures";
import { modules, NOW } from "./test.setup";

const HOUR = 3_600_000;
const INVALID_KEY = { error: "unauthorized", message: "Invalid API key" };

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

async function keyOf(f: Awaited<ReturnType<typeof machineFixture>>) {
  const key = await f.t.run((ctx) =>
    ctx.db
      .query("machine_api_keys")
      .withIndex("by_machine", (q) => q.eq("machineId", f.machineId))
      .unique(),
  );
  if (key === null) throw new Error("Missing fixture key");
  return key;
}

function request(apiKey: string) {
  return new Request("https://machine.example.invalid/api/machine/heartbeat", {
    headers: machineHeaders(apiKey),
  });
}

describe("ANH-238 the action judges time even with a cached query", () => {
  it.each([
    ["authenticateMachineRequest", authenticateMachineRequest],
    ["validateMachineAuth", validateMachineAuth],
  ] as const)(
    "%s refuses at expiry and afterwards, without recording a use",
    async (_name, gate) => {
      const f = await machineFixture(modules);
      const key = await keyOf(f);
      await f.t.run((ctx) => ctx.db.patch(key._id, { expiresAt: NOW + HOUR }));
      const captured = await f.t.query(internal.machines.getMachineByApiKey, {
        apiKey: f.apiKey,
      });
      for (const offset of [-1, 0, 1]) {
        vi.setSystemTime(NOW + HOUR + offset);
        await f.t.action(async (ctx) => {
          vi.spyOn(ctx, "runQuery").mockResolvedValue(captured);
          const writes = vi.spyOn(ctx, "runMutation");
          const result = await gate(ctx, request(f.apiKey));
          if (offset < 0) {
            expect(result).toHaveProperty("machine._id", f.machineId);
            expect(writes).toHaveBeenCalledOnce();
          } else {
            if (!("error" in result)) throw new Error("Expired key accepted");
            expect(result.error.status).toBe(401);
            expect(await result.error.json()).toEqual(INVALID_KEY);
            expect(writes).not.toHaveBeenCalled();
          }
        });
      }
    },
  );

  it("records a cached last use at one minute, but not one millisecond before", async () => {
    const f = await machineFixture(modules);
    const key = await keyOf(f);
    await f.t.run((ctx) => ctx.db.patch(key._id, { lastUsedAt: NOW }));
    const captured = await f.t.query(internal.machines.getMachineByApiKey, {
      apiKey: f.apiKey,
    });
    for (const offset of [-1, 0]) {
      vi.setSystemTime(NOW + KEY_USE_REFRESH_MS + offset);
      await f.t.action(async (ctx) => {
        vi.spyOn(ctx, "runQuery").mockResolvedValue(captured);
        const writes = vi.spyOn(ctx, "runMutation");
        expect(
          await authenticateMachineRequest(ctx, request(f.apiKey)),
        ).toHaveProperty("machine._id", f.machineId);
        expect(writes).toHaveBeenCalledTimes(offset < 0 ? 0 : 1);
      });
      expect((await keyOf(f)).lastUsedAt).toBe(
        offset < 0 ? NOW : NOW + KEY_USE_REFRESH_MS,
      );
    }
  });

  it("refuses a cached expired key on all 13 routes before the contract or bucket", async () => {
    let captured: Awaited<ReturnType<typeof authenticateMachine>> = null;
    const f = await machineFixture({
      ...modules,
      "./machines.ts": async () => ({
        ...(await import("./machines")),
        getMachineByApiKey: internalQuery({
          args: { apiKey: v.string() },
          returns: authenticatedMachine,
          handler: (ctx, args) =>
            captured ?? authenticateMachine(ctx, args.apiKey),
        }),
      }),
    });
    const key = await keyOf(f);
    await f.t.run((ctx) => ctx.db.patch(key._id, { expiresAt: NOW + HOUR }));
    captured = await f.t.query(internal.machines.validateMachineApiKey, {
      apiKey: f.apiKey,
    });
    vi.setSystemTime(NOW + HOUR);
    const limit = vi.spyOn(rateLimiter, "limit");
    const headers = machineHeaders(f.apiKey);
    headers[CONTRACT_HEADER] = "2.0";
    for (const [method, path] of machineRoutes) {
      const response = await f.t.fetch(path, { method, headers });
      expect(response.status, path).toBe(401);
      expect(await response.json()).toEqual(INVALID_KEY);
    }
    expect(limit).not.toHaveBeenCalled();
    expect((await keyOf(f)).lastUsedAt).toBeUndefined();
    captured = null;
    await f.t.run((ctx) => ctx.db.patch(key._id, { expiresAt: undefined }));
    expect(
      (
        await f.t.fetch("/api/machine/heartbeat", {
          method: "POST",
          headers: machineHeaders(f.apiKey),
        })
      ).status,
    ).toBe(200);
  });

  it.each(["expired", "replaced"])(
    "refuses a key that is %s through both queries and every route",
    async (state) => {
      const f = await machineFixture(modules);
      const key = await keyOf(f);
      if (state === "replaced") {
        await f.admin.mutation(api.machineKeys.createMachineKey, {
          machineId: f.machineId,
          overlapMinutes: 60,
        });
      } else {
        await f.t.run((ctx) =>
          ctx.db.patch(key._id, { expiresAt: NOW + HOUR }),
        );
      }
      vi.setSystemTime(NOW + HOUR);
      for (const query of [
        internal.machines.getMachineByApiKey,
        internal.machines.validateMachineApiKey,
      ]) {
        expect(await f.t.query(query, { apiKey: f.apiKey })).toBeNull();
      }
      for (const [method, path] of machineRoutes) {
        const response = await f.t.fetch(path, {
          method,
          headers: machineHeaders(f.apiKey),
        });
        expect(response.status, path).toBe(401);
        expect(await response.json()).toEqual(INVALID_KEY);
      }
    },
  );

  it("never opens a gate when the authentication query itself fails", async () => {
    const f = await machineFixture(modules);
    await f.t.action(async (ctx) => {
      vi.spyOn(ctx, "runQuery").mockRejectedValue(
        new Error("Synthetic query failure"),
      );
      const writes = vi.spyOn(ctx, "runMutation");
      await expect(validateMachineAuth(ctx, request(f.apiKey))).rejects.toThrow(
        "Synthetic query failure",
      );
      expect(writes).not.toHaveBeenCalled();
    });
  });
});

describe("ANH-238 a failed use record never changes a machine response", () => {
  it.each(machineRoutes)(
    "%s %s keeps its response and catches up next time",
    async (method, path) => {
      let failUse = false;
      let failureMessage = "";
      const f = await machineFixture({
        ...modules,
        "./machineKeys.ts": async () => {
          const original = await import("./machineKeys");
          return {
            ...original,
            recordKeyUse: failUse
              ? internalMutation({
                  args: { keyId: v.id("machine_api_keys") },
                  returns: v.boolean(),
                  handler: () => {
                    throw new Error(failureMessage);
                  },
                })
              : original.recordKeyUse,
          };
        },
      });
      const key = await keyOf(f);
      // Even an exception containing the credential must never reach the log.
      failureMessage = `${f.apiKey} ${key.selector} ${key.keyHash}`;
      const sessionId = await f.t.mutation(
        internal.training.registerLocalSession,
        {
          machineId: f.machineId,
          localRef: "synthetic-clock",
          kind: "manual",
          startedAt: NOW,
          operatorName: "Synthetic Operator",
        },
      );
      if (path.endsWith("training/status")) {
        await f.admin.mutation(api.training.requestStop, { sessionId });
      }
      const route = path.endsWith("/status")
        ? `${path}?sessionId=${sessionId}`
        : path;
      const body = path.endsWith("/end")
        ? { sessionId, failed: false, reason: "synthetic_stop" }
        : {};
      const send = () =>
        f.t.fetch(route, {
          method,
          headers: machineHeaders(f.apiKey),
          body: method === "POST" ? JSON.stringify(body) : undefined,
        });
      const normal = await send();
      const expected = await normal.json();
      if (path.endsWith("training/status") || path.endsWith("training/end")) {
        expect(normal.status).toBe(200);
      }
      if (path.endsWith("training/status")) {
        expect(expected).toMatchObject({ stopRequested: true });
      }
      const limits: MachineLimit[] = [
        "machineHeartbeat",
        "machinePoll",
        "machineTelemetry",
        "machineEvents",
        "machineUploadUrl",
        "machineLocalSession",
        "machineProfiles",
        "machineRecordCommit",
      ];
      await f.t.mutation(async (ctx) => {
        for (const name of limits)
          await rateLimiter.reset(ctx, name, { key: f.machineId });
      });
      await f.t.run((ctx) => ctx.db.patch(key._id, { lastUsedAt: undefined }));
      if (path.endsWith("training/end")) {
        await f.t.run((ctx) =>
          ctx.db.patch(sessionId, { status: "active", endedAt: undefined }),
        );
      }
      failUse = true;
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const failed = await send();
      expect(failed.status).toBe(normal.status);
      expect(await failed.json()).toEqual(expected);
      if (path.endsWith("training/end")) {
        expect((await f.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
          "completed",
        );
      }
      expect((await keyOf(f)).lastUsedAt).toBeUndefined();
      expect(warn).toHaveBeenCalledOnce();
      expect(warn.mock.calls[0]).toHaveLength(1);
      const logged = JSON.stringify(warn.mock.calls);
      for (const secret of [
        f.apiKey,
        key.selector,
        key.keyHash,
        "Authorization",
      ]) {
        expect(logged).not.toContain(secret);
      }
      expect(logged).not.toContain("\\n");
      failUse = false;
      expect(
        (
          await f.t.fetch("/api/machine/roster", {
            method: "GET",
            headers: machineHeaders(f.apiKey),
          })
        ).status,
      ).toBe(200);
      expect((await keyOf(f)).lastUsedAt).toBe(NOW);
    },
  );
});

describe("ANH-238 a verified key is still used before the contract gate", () => {
  it.each([undefined, "2.0"])(
    "a %s contract records the use but spends no heartbeat token",
    async (contract) => {
      const f = await machineFixture(modules);
      const headers = machineHeaders(f.apiKey);
      if (contract === undefined) delete headers[CONTRACT_HEADER];
      else headers[CONTRACT_HEADER] = contract;
      const refused = await f.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers,
      });
      expect(refused.status).toBe(426);
      expect((await refused.json()).error).toBe("contract_unsupported");
      expect((await keyOf(f)).lastUsedAt).toBe(NOW);
      expect(
        (
          await f.t.fetch("/api/machine/heartbeat", {
            method: "POST",
            headers: machineHeaders(f.apiKey),
          })
        ).status,
      ).toBe(200);
    },
  );

  it("an unserved stop contract records the key and still delivers the stop", async () => {
    const f = await machineFixture(modules);
    const sessionId = await f.t.mutation(
      internal.training.registerLocalSession,
      {
        machineId: f.machineId,
        localRef: "synthetic-stop-clock",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Synthetic Operator",
      },
    );
    await f.admin.mutation(api.training.requestStop, { sessionId });
    const headers = machineHeaders(f.apiKey);
    headers[CONTRACT_HEADER] = "2.0";
    const response = await f.t.fetch(
      `/api/machine/training/status?sessionId=${sessionId}`,
      { method: "GET", headers },
    );
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ stopRequested: true });
    expect((await keyOf(f)).lastUsedAt).toBe(NOW);
  });
});
