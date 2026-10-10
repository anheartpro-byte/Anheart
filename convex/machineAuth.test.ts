/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import {
  machineFixture,
  machineHeaders,
  machineRoutes,
} from "./machineAuth.fixtures";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
]);

afterEach(() => vi.restoreAllMocks());

describe("ANH-121 machine authentication", () => {
  it.each(machineRoutes)(
    "rejects a deleted machine through all authentication entry points: %s %s",
    async (method, path) => {
      // Given a real credential whose machine has been deleted.
      const f = await machineFixture(modules);
      await f.admin.mutation(api.machines.deleteMachine, {
        machineId: f.machineId,
      });
      // When the credential reaches the registered HTTP route.
      const response = await f.t.fetch(path, {
        method,
        headers: { Authorization: `Bearer ${f.apiKey}` },
      });
      // Then authentication fails before request processing.
      expect(response.status).toBe(401);
    },
  );

  it("rejects a deleted machine through the raw-key authentication query", async () => {
    // Given a deleted synthetic machine.
    const f = await machineFixture(modules);
    await f.admin.mutation(api.machines.deleteMachine, {
      machineId: f.machineId,
    });
    // When / Then the registered authentication query rejects its key.
    expect(
      await f.t.query(internal.machines.validateMachineApiKey, {
        apiKey: f.apiKey,
      }),
    ).toBeNull();
  });

  it("does not store a reversible credential", async () => {
    // Given a newly created synthetic machine.
    const f = await machineFixture(modules);
    // When reading its persisted record.
    const stored = await f.t.run((ctx) => ctx.db.get(f.machineId));
    // Then neither plaintext nor the reversible legacy encoding is stored.
    expect(stored?.apiKey === f.apiKey).toBe(false);
    expect(stored?.apiKey === btoa(f.apiKey).split("").reverse().join("")).toBe(
      false,
    );
  });

  it.each(machineRoutes)(
    "rejects an explicitly disabled machine: %s %s",
    async (method, path) => {
      // Given an explicitly disabled machine, independently of its connection status.
      const f = await machineFixture(modules);
      await f.t.run((ctx) =>
        ctx.db.patch(f.machineId, { authenticationEnabled: false }),
      );
      // When / Then the registered route refuses authentication.
      const response = await f.t.fetch(path, {
        method,
        headers: { Authorization: `Bearer ${f.apiKey}` },
      });
      expect(response.status).toBe(401);
    },
  );

  it.each([
    { state: "deleted", patch: { isDeleted: true } },
    { state: "disabled", patch: { authenticationEnabled: false } },
  ])(
    "rejects a $state machine through both authentication queries",
    async ({ patch }) => {
      // Given a machine whose permission to authenticate has been withdrawn.
      const f = await machineFixture(modules);
      await f.t.run((ctx) => ctx.db.patch(f.machineId, patch));
      // When / Then both registered queries use the same rejection policy.
      for (const query of [
        internal.machines.getMachineByApiKey,
        internal.machines.validateMachineApiKey,
      ]) {
        expect(await f.t.query(query, { apiKey: f.apiKey })).toBeNull();
      }
    },
  );

  it("accepts a valid key from an ordinary offline machine", async () => {
    // Given a new, offline machine with no explicit disabling flag.
    const f = await machineFixture(modules);
    // When the machine authenticates through both queries and sends a heartbeat.
    const first = await f.t.query(internal.machines.getMachineByApiKey, {
      apiKey: f.apiKey,
    });
    const second = await f.t.query(internal.machines.validateMachineApiKey, {
      apiKey: f.apiKey,
    });
    const response = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: machineHeaders(f.apiKey),
    });
    // Then authentication succeeds and the actual heartbeat is persisted.
    expect(first?._id).toBe(f.machineId);
    expect(second?._id).toBe(f.machineId);
    expect(response.status).toBe(200);
    const beats = await f.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(beats.map((beat) => beat.machineId)).toEqual([f.machineId]);
  });

  it("rejects the previous key after regeneration", async () => {
    // Given a machine whose key has been replaced through the real mutation.
    const f = await machineFixture(modules);
    const replacement = await f.admin.mutation(api.machines.regenerateApiKey, {
      machineId: f.machineId,
    });
    // When the old key reaches every registered authentication entry point.
    for (const [method, path] of machineRoutes) {
      const response = await f.t.fetch(path, {
        method,
        headers: { Authorization: `Bearer ${f.apiKey}` },
      });
      expect(response.status).toBe(401);
    }
    for (const query of [
      internal.machines.getMachineByApiKey,
      internal.machines.validateMachineApiKey,
    ]) {
      expect(await f.t.query(query, { apiKey: f.apiKey })).toBeNull();
    }
    // Then only the replacement key authenticates and retains the one-time response shape.
    expect(Object.keys(replacement)).toEqual(["apiKey"]);
    const response = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: machineHeaders(replacement.apiKey),
    });
    expect(response.status).toBe(200);
  });

  it("returns credentials only in creation and regeneration responses, never in reads or logs", async () => {
    // Given real creation and regeneration while observing application logging.
    const logs = [
      vi.spyOn(console, "log"),
      vi.spyOn(console, "warn"),
      vi.spyOn(console, "error"),
    ];
    const f = await machineFixture(modules);
    const replacement = await f.admin.mutation(api.machines.regenerateApiKey, {
      machineId: f.machineId,
    });
    // When all machine detail and list readers are queried.
    const reads = [
      await f.admin.query(api.machines.getMachine, { machineId: f.machineId }),
      await f.admin.query(api.machines.listMachines, {}),
      await f.t.query(internal.machines.getMachineByApiKey, {
        apiKey: replacement.apiKey,
      }),
      await f.t.query(internal.machines.validateMachineApiKey, {
        apiKey: replacement.apiKey,
      }),
    ];
    const persisted = await f.t.run((ctx) => ctx.db.get(f.machineId));
    // Then neither the original nor replacement credential is available for redisplay or logging.
    for (const output of [
      reads,
      persisted,
      logs.map((spy) => spy.mock.calls),
    ]) {
      const serialized = JSON.stringify(output);
      expect(serialized.includes(f.apiKey)).toBe(false);
      expect(serialized.includes(replacement.apiKey)).toBe(false);
    }
    for (const read of reads)
      expect(JSON.stringify(read).includes('"apiKey"')).toBe(false);
  });

  it.each([
    undefined,
    "Basic synthetic",
    "Bearer ",
    "Bearer malformed",
    `Bearer ${"a".repeat(64)}`,
  ])(
    "rejects a missing, malformed, or legacy authorization header (%#)",
    async (authorization) => {
      // Given a valid registered machine and an invalid request credential.
      const f = await machineFixture(modules);
      // When / Then heartbeat authentication fails without processing its body.
      const response = await f.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers: authorization ? { Authorization: authorization } : {},
      });
      expect(response.status).toBe(401);
    },
  );

  it("rejects a known selector with a wrong secret", async () => {
    // Given the non-secret selector but a different secret.
    const f = await machineFixture(modules);
    const wrong = f.apiKey.slice(0, -1) + (f.apiKey.endsWith("0") ? "1" : "0");
    // When / Then both query entry points and heartbeat verify the secret.
    for (const query of [
      internal.machines.getMachineByApiKey,
      internal.machines.validateMachineApiKey,
    ]) {
      expect(await f.t.query(query, { apiKey: wrong })).toBeNull();
    }
    const response = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: { Authorization: `Bearer ${wrong}` },
    });
    expect(response.status).toBe(401);
  });
});
