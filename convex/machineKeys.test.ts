/// <reference types="vite/client" />
/**
 * ANH-165: the keys of a machine. Several keys can be valid at once (the
 * overlap of a rotation), each one is revoked on its own and may expire, the
 * plain key is shown once, and the single key of a machine written before is
 * moved into the table without ever being refused.
 *
 * Every assertion goes through the registered functions and HTTP routes, on
 * an in-memory backend with a fixed clock (`Date` only: the routes' rate
 * limiter counts on it). The roles are the matrix's
 * (`authorization.matrix.ts`); what is proven here is what a key is worth.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import crons from "./crons";
import { generateApiKey } from "./lib/crypto";
import {
  DEFAULT_OVERLAP_MINUTES,
  KEY_USE_REFRESH_MS,
  keyPrefix,
  LISTED_KEYS,
  MAX_OVERLAP_MINUTES,
  MAX_KEY_LIFETIME_MINUTES,
  MIN_KEY_LIFETIME_MINUTES,
  rotationOptions,
} from "./lib/machineKeys";
import {
  machineFixture,
  machineHeaders,
  machineRoutes,
} from "./machineAuth.fixtures";
import { as, backendOf, modules, NOW, seedWorld } from "./test.setup";

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const OVERLAP = DEFAULT_OVERLAP_MINUTES * 60_000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

type Fixture = Awaited<ReturnType<typeof machineFixture>>;

/**
 * The status of an authenticated request that has no rate limit and writes
 * nothing of its own (the roster): 200 when the key opens the machine
 * routes, 401 when it does not.
 */
async function status(f: Fixture, apiKey: string): Promise<number> {
  const response = await f.t.fetch("/api/machine/roster", {
    method: "GET",
    headers: machineHeaders(apiKey),
  });
  return response.status;
}

async function keysOf(f: Fixture, machineId: Id<"machines"> = f.machineId) {
  return await f.t.run((ctx) =>
    ctx.db
      .query("machine_api_keys")
      .withIndex("by_machine", (q) => q.eq("machineId", machineId))
      .collect(),
  );
}

async function keyOf(f: Fixture, keyId: Id<"machine_api_keys">) {
  const key = await f.t.run((ctx) => ctx.db.get(keyId));
  if (key === null) throw new Error("No such key");
  return key;
}

/** The row of the key the fixture's machine was created with. */
async function firstKey(f: Fixture) {
  const [key] = await keysOf(f);
  if (key === undefined) throw new Error("The machine has no key");
  return key;
}

function rotate(
  f: Fixture,
  args: { overlapMinutes?: number; lifetimeMinutes?: number } = {},
) {
  return f.admin.mutation(api.machineKeys.createMachineKey, {
    machineId: f.machineId,
    ...args,
  });
}

function at(time: number) {
  vi.setSystemTime(time);
}

describe("ANH-165 EX-1 a key lives in machine_api_keys", () => {
  it("creates the machine's first key in the table, and nothing of it on the machine", async () => {
    const f = await machineFixture(modules);

    const machine = await f.t.run((ctx) => ctx.db.get(f.machineId));
    const [key, ...others] = await keysOf(f);
    expect(others).toEqual([]);
    expect(machine?.apiKey).toBeUndefined();
    expect(machine?.apiKeySelector).toBeUndefined();
    expect(key.selector).toBe(f.apiKey.split(".")[1]);
    expect(key.prefix).toBe(keyPrefix(key.selector));
    expect(f.apiKey.startsWith(key.prefix)).toBe(true);
    expect(key.keyHash.startsWith("hmac-sha256:1:")).toBe(true);
    expect(JSON.stringify(key).includes(f.apiKey)).toBe(false);
    expect(key.createdAt).toBe(NOW);
    expect(key.createdBy).toBeDefined();
    expect(key.expiresAt).toBeUndefined();
    expect(key.revokedAt).toBeUndefined();
    expect(await status(f, f.apiKey)).toBe(200);
  });

  it("refuses a key whose selector is known but whose secret is not", async () => {
    const f = await machineFixture(modules);
    const wrong = f.apiKey.slice(0, -1) + (f.apiKey.endsWith("0") ? "1" : "0");

    expect(await status(f, wrong)).toBe(401);
    expect(
      await f.t.query(internal.machines.getMachineByApiKey, { apiKey: wrong }),
    ).toBeNull();
  });

  it.each([
    ["deleted", { isDeleted: true }],
    ["disabled", { authenticationEnabled: false }],
  ] as const)(
    "refuses a valid key of a %s machine on every machine route",
    async (_state, patch) => {
      const f = await machineFixture(modules);
      await f.t.run((ctx) => ctx.db.patch(f.machineId, patch));

      for (const [method, path] of machineRoutes) {
        const response = await f.t.fetch(path, {
          method,
          headers: machineHeaders(f.apiKey),
        });
        expect(response.status, `${method} ${path}`).toBe(401);
      }
      expect(
        await f.t.query(internal.machines.getMachineByApiKey, {
          apiKey: f.apiKey,
        }),
      ).toBeNull();
    },
  );

  it("opens the routes again to the keys of a restored machine, as before", async () => {
    const f = await machineFixture(modules);
    await f.admin.mutation(api.machines.deleteMachine, {
      machineId: f.machineId,
    });
    expect(await status(f, f.apiKey)).toBe(401);

    await f.admin.mutation(api.machines.restoreMachine, {
      machineId: f.machineId,
    });

    expect(await status(f, f.apiKey)).toBe(200);
  });
});

describe("ANH-165 EX-2 a rotation keeps both keys valid during the overlap", () => {
  it("answers the new key once, and keeps the old one valid for 24 h by default", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    const issued = await rotate(f);

    expect(Object.keys(issued).sort()).toEqual(["apiKey", "keyId", "prefix"]);
    expect(issued.apiKey.startsWith(issued.prefix)).toBe(true);
    expect(await status(f, f.apiKey)).toBe(200);
    expect(await status(f, issued.apiKey)).toBe(200);
    const retired = await keyOf(f, old._id);
    expect(retired.expiresAt).toBe(NOW + OVERLAP);
    expect(retired.replacedBy).toBe(issued.keyId);
    expect(retired.revokedAt).toBeUndefined();
    const fresh = await keyOf(f, issued.keyId);
    expect(fresh.expiresAt).toBeUndefined();
    expect(fresh.replacedBy).toBeUndefined();
  });

  it("refuses the old key from the end of the overlap on, and only it", async () => {
    const f = await machineFixture(modules);
    const issued = await rotate(f);

    at(NOW + OVERLAP - 1);
    expect(await status(f, f.apiKey)).toBe(200);
    at(NOW + OVERLAP);
    expect(await status(f, f.apiKey)).toBe(401);
    expect(await status(f, issued.apiKey)).toBe(200);
  });

  it("goes on with heartbeats through the change of key: old key, both, new key", async () => {
    const f = await machineFixture(modules);
    const beat = async (apiKey: string, time: number) => {
      at(time);
      const response = await f.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers: machineHeaders(apiKey),
      });
      return response.status;
    };

    expect(await beat(f.apiKey, NOW)).toBe(200);
    const issued = await rotate(f, { overlapMinutes: 60 });
    expect(await beat(f.apiKey, NOW + 10_000)).toBe(200);
    expect(await beat(issued.apiKey, NOW + 20_000)).toBe(200);
    expect(await beat(issued.apiKey, NOW + HOUR)).toBe(200);
    expect(await beat(f.apiKey, NOW + HOUR + 10_000)).toBe(401);

    const beats = await f.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(beats.map((b) => b.timestamp)).toEqual([
      NOW,
      NOW + 10_000,
      NOW + 20_000,
      NOW + HOUR,
    ]);
  });

  it("revokes the old keys at once with an overlap of 0", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    const issued = await rotate(f, { overlapMinutes: 0 });

    expect(await status(f, f.apiKey)).toBe(401);
    expect(await status(f, issued.apiKey)).toBe(200);
    const revoked = await keyOf(f, old._id);
    expect(revoked.revokedAt).toBe(NOW);
    expect(revoked.revokedBy).toBe(old.createdBy);
    expect(revoked.replacedBy).toBe(issued.keyId);
  });

  it("never lengthens a key that expires before the end of the overlap", async () => {
    const f = await machineFixture(modules);
    const short = await rotate(f, {
      overlapMinutes: 0,
      lifetimeMinutes: 2 * 60,
    });
    const long = await rotate(f, { overlapMinutes: 0 });
    const latest = await rotate(f, { overlapMinutes: 3 * 60 });

    // `short` was revoked by the second rotation: untouched by the third.
    expect((await keyOf(f, short.keyId)).revokedAt).toBe(NOW);
    expect((await keyOf(f, long.keyId)).expiresAt).toBe(NOW + 3 * HOUR);
    expect((await keyOf(f, latest.keyId)).expiresAt).toBeUndefined();

    const f2 = await machineFixture(modules);
    const expiring = await rotate(f2, {
      overlapMinutes: 0,
      lifetimeMinutes: 2 * 60,
    });
    await rotate(f2);
    expect((await keyOf(f2, expiring.keyId)).expiresAt).toBe(NOW + 2 * HOUR);
  });

  it("leaves an expired key to the job instead of retiring it again", async () => {
    const f = await machineFixture(modules);
    const expiring = await rotate(f, {
      overlapMinutes: 0,
      lifetimeMinutes: 60,
    });

    at(NOW + 2 * HOUR);
    await rotate(f, { overlapMinutes: 0 });

    const key = await keyOf(f, expiring.keyId);
    expect(key.revokedAt).toBeUndefined();
    expect(key.replacedBy).toBeUndefined();
    expect(key.expiresAt).toBe(NOW + HOUR);
  });

  it("touches no key of another machine", async () => {
    const f = await machineFixture(modules);
    const other = await f.admin.mutation(api.machines.createMachine, {
      name: "Another synthetic machine",
    });
    const before = await keysOf(f, other.machineId);

    await rotate(f, { overlapMinutes: 0 });

    expect(await keysOf(f, other.machineId)).toEqual(before);
    expect(await status(f, other.apiKey)).toBe(200);
  });

  it("issues no key for a machine that does not exist any more", async () => {
    const f = await machineFixture(modules);
    await f.t.run((ctx) => ctx.db.delete(f.machineId));

    await expect(rotate(f)).rejects.toThrow(/machine_not_found/);
  });
});

describe("ANH-165 EX-2 the job revokes the keys whose expiry passed", () => {
  it("is registered to run every 5 minutes", () => {
    const registered = (
      crons as unknown as {
        crons: Record<string, { name: string; schedule: unknown }>;
      }
    ).crons["revoke-expired-machine-keys"];
    expect(registered?.name).toBe("machineKeys:revokeExpiredKeys");
    expect(registered?.schedule).toEqual({ type: "interval", minutes: 5 });
  });

  it("revokes each key once its expiry passed, and no key before", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    const issued = await rotate(f);
    // A third key, with an expiry, and the longest overlap: `old` keeps the
    // end of its own overlap, `issued` gets 7 days.
    const expiring = await rotate(f, {
      overlapMinutes: MAX_OVERLAP_MINUTES,
      lifetimeMinutes: 2 * 24 * 60,
    });
    const revokeExpired = () =>
      f.t.mutation(internal.machineKeys.revokeExpiredKeys, {});

    at(NOW + OVERLAP - 1);
    expect(await revokeExpired()).toEqual({ revoked: 0 });

    at(NOW + OVERLAP);
    expect(await revokeExpired()).toEqual({ revoked: 1 });
    expect((await keyOf(f, old._id)).revokedAt).toBe(NOW + OVERLAP);
    expect((await keyOf(f, old._id)).revokedBy).toBeUndefined();

    at(NOW + 2 * DAY);
    expect(await revokeExpired()).toEqual({ revoked: 1 });
    expect((await keyOf(f, expiring.keyId)).revokedAt).toBe(NOW + 2 * DAY);
    expect((await keyOf(f, issued.keyId)).revokedAt).toBeUndefined();
    expect(await revokeExpired()).toEqual({ revoked: 0 });

    at(NOW + 7 * DAY);
    expect(await revokeExpired()).toEqual({ revoked: 1 });
    expect((await keyOf(f, issued.keyId)).revokedAt).toBe(NOW + 7 * DAY);
  });

  it("never revokes a key without expiry", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    at(NOW + 365 * DAY);
    expect(
      await f.t.mutation(internal.machineKeys.revokeExpiredKeys, {}),
    ).toEqual({ revoked: 0 });
    expect((await keyOf(f, old._id)).revokedAt).toBeUndefined();
    expect(await status(f, f.apiKey)).toBe(200);
  });
});

describe("ANH-165 EX-2 a key is revoked at once", () => {
  it("refuses the revoked key on every machine route from the next request, and keeps the other key", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    const issued = await rotate(f);

    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: issued.keyId,
    });

    for (const [method, path] of machineRoutes) {
      const response = await f.t.fetch(path, {
        method,
        headers: machineHeaders(issued.apiKey),
      });
      expect(response.status, `${method} ${path}`).toBe(401);
    }
    for (const query of [
      internal.machines.getMachineByApiKey,
      internal.machines.validateMachineApiKey,
    ]) {
      expect(await f.t.query(query, { apiKey: issued.apiKey })).toBeNull();
    }
    expect(await status(f, f.apiKey)).toBe(200);
    const revoked = await keyOf(f, issued.keyId);
    expect(revoked.revokedAt).toBe(NOW);
    expect(revoked.revokedBy).toBe(old.createdBy);
  });

  it("is refused 401 before the machine's rate limit is counted", async () => {
    const f = await machineFixture(modules);
    const issued = await rotate(f);
    const heartbeat = (apiKey: string) =>
      f.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers: machineHeaders(apiKey),
      });
    expect((await heartbeat(f.apiKey)).status).toBe(200);
    expect((await heartbeat(f.apiKey)).status).toBe(429);

    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: issued.keyId,
    });

    expect((await heartbeat(issued.apiKey)).status).toBe(401);
  });

  it("leaves an already revoked key as it was", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: old._id,
    });
    const once = await keyOf(f, old._id);

    at(NOW + HOUR);
    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: old._id,
    });

    expect(await keyOf(f, old._id)).toEqual(once);
  });

  it("revokes a key of a deleted machine", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await f.admin.mutation(api.machines.deleteMachine, {
      machineId: f.machineId,
    });

    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: old._id,
    });

    expect((await keyOf(f, old._id)).revokedAt).toBe(NOW);
  });

  it("answers a key that does not exist as one of another organisation", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await f.t.run((ctx) => ctx.db.delete(old._id));

    await expect(
      f.admin.mutation(api.machineKeys.revokeMachineKey, { keyId: old._id }),
    ).rejects.toThrow(/machine_key_not_found/);
  });

  it("answers a key whose machine is gone as a key that does not exist", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await f.t.run((ctx) => ctx.db.delete(f.machineId));

    await expect(
      f.admin.mutation(api.machineKeys.revokeMachineKey, { keyId: old._id }),
    ).rejects.toThrow(/machine_key_not_found/);
  });
});

describe("ANH-165 EX-1 a key may expire", () => {
  it("opens the routes until its expiry, and nothing from then on", async () => {
    const f = await machineFixture(modules);
    const issued = await rotate(f, {
      overlapMinutes: 0,
      lifetimeMinutes: 2 * 60,
    });
    expect((await keyOf(f, issued.keyId)).expiresAt).toBe(NOW + 2 * HOUR);

    at(NOW + 2 * HOUR - 1);
    expect(await status(f, issued.apiKey)).toBe(200);
    at(NOW + 2 * HOUR);
    expect(await status(f, issued.apiKey)).toBe(401);
  });

  it.each([
    [{ lifetimeMinutes: MIN_KEY_LIFETIME_MINUTES - 1 }, /invalid_key_expiry/],
    [{ lifetimeMinutes: 90.5 }, /invalid_key_expiry/],
    [{ lifetimeMinutes: MAX_KEY_LIFETIME_MINUTES + 1 }, /invalid_key_expiry/],
    [{ lifetimeMinutes: -60 }, /invalid_key_expiry/],
    [{ overlapMinutes: -1 }, /invalid_key_overlap/],
    [{ overlapMinutes: 1.5 }, /invalid_key_overlap/],
    [{ overlapMinutes: MAX_OVERLAP_MINUTES + 1 }, /invalid_key_overlap/],
  ])("refuses %o and writes nothing", async (args, refusal) => {
    const f = await machineFixture(modules);
    const before = await keysOf(f);

    await expect(rotate(f, args)).rejects.toThrow(refusal);

    expect(await keysOf(f)).toEqual(before);
  });

  it("accepts the bounds: one hour of life, an overlap of 7 days", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    const issued = await rotate(f, {
      overlapMinutes: MAX_OVERLAP_MINUTES,
      lifetimeMinutes: MIN_KEY_LIFETIME_MINUTES,
    });

    expect((await keyOf(f, issued.keyId)).expiresAt).toBe(NOW + HOUR);
    expect((await keyOf(f, old._id)).expiresAt).toBe(NOW + 7 * DAY);
    const longest = await rotate(f, {
      lifetimeMinutes: MAX_KEY_LIFETIME_MINUTES,
    });
    expect((await keyOf(f, longest.keyId)).expiresAt).toBe(NOW + 3650 * DAY);
  });

  it("counts the lifetime on the server's clock, and refuses one that is not a number of minutes", () => {
    expect(rotationOptions({ lifetimeMinutes: 90 * 24 * 60 }, NOW)).toEqual({
      overlapMs: OVERLAP,
      expiresAt: NOW + 90 * DAY,
    });
    expect(rotationOptions({}, NOW)).toEqual({
      overlapMs: OVERLAP,
      expiresAt: undefined,
    });
    for (const lifetimeMinutes of [Number.NaN, Number.POSITIVE_INFINITY]) {
      expect(() => rotationOptions({ lifetimeMinutes }, NOW)).toThrow(
        /invalid_key_expiry/,
      );
    }
    expect(() => rotationOptions({ overlapMinutes: Number.NaN }, NOW)).toThrow(
      /invalid_key_overlap/,
    );
  });
});

describe("ANH-165 EX-4 the use of a key is written once a minute at most", () => {
  it("writes the first use, then nothing before a minute has passed", async () => {
    const f = await machineFixture(modules);
    const key = await firstKey(f);
    expect(key.lastUsedAt).toBeUndefined();

    expect(await status(f, f.apiKey)).toBe(200);
    expect((await keyOf(f, key._id)).lastUsedAt).toBe(NOW);

    for (const later of [1, 30_000, KEY_USE_REFRESH_MS - 1]) {
      at(NOW + later);
      expect(await status(f, f.apiKey)).toBe(200);
      expect((await keyOf(f, key._id)).lastUsedAt).toBe(NOW);
    }

    at(NOW + KEY_USE_REFRESH_MS);
    expect(await status(f, f.apiKey)).toBe(200);
    expect((await keyOf(f, key._id)).lastUsedAt).toBe(NOW + KEY_USE_REFRESH_MS);
  });

  it("authenticates without writing: the query returns the raw dates for the action", async () => {
    const f = await machineFixture(modules);
    const key = await firstKey(f);
    await f.t.run((ctx) => ctx.db.patch(key._id, { expiresAt: NOW + HOUR }));
    const authenticate = () =>
      f.t.query(internal.machines.getMachineByApiKey, { apiKey: f.apiKey });

    expect((await authenticate())?.key).toEqual({
      id: key._id,
      expiresAt: NOW + HOUR,
    });
    expect((await keyOf(f, key._id)).lastUsedAt).toBeUndefined();

    expect(
      await f.t.mutation(internal.machineKeys.recordKeyUse, { keyId: key._id }),
    ).toBe(true);
    at(NOW + 30_000);
    expect((await authenticate())?.key).toEqual({
      id: key._id,
      expiresAt: NOW + HOUR,
      lastUsedAt: NOW,
    });
    expect(
      await f.t.mutation(internal.machineKeys.recordKeyUse, { keyId: key._id }),
    ).toBe(false);
    expect((await keyOf(f, key._id)).lastUsedAt).toBe(NOW);
  });

  it("writes nothing for a key that is gone", async () => {
    const f = await machineFixture(modules);
    const key = await firstKey(f);
    await f.t.run((ctx) => ctx.db.delete(key._id));

    expect(
      await f.t.mutation(internal.machineKeys.recordKeyUse, { keyId: key._id }),
    ).toBe(false);
  });

  it("writes the use of the key that opened the request, not of the others", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    const issued = await rotate(f);

    expect(await status(f, issued.apiKey)).toBe(200);

    expect((await keyOf(f, issued.keyId)).lastUsedAt).toBe(NOW);
    expect((await keyOf(f, old._id)).lastUsedAt).toBeUndefined();
  });
});

describe("ANH-165 EX-3 what the machine's page reads of its keys", () => {
  it("lists all 56 unrevoked keys after 55 rotations in the same overlap", async () => {
    const f = await machineFixture(modules);
    const ids = [(await firstKey(f))._id];
    for (let n = 0; n < 55; n++) {
      const issued = await f.t.mutation(
        internal.machineKeys.createKeyByOperator,
        {
          machineId: f.machineId,
        },
      );
      ids.unshift(issued.keyId);
    }

    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: f.machineId,
    });

    expect(listed).toHaveLength(56);
    expect(
      listed.map((key) => (key.kind === "table" ? key._id : "legacy")),
    ).toEqual(ids);
    expect(
      listed.every((key) => ["active", "replaced"].includes(key.state)),
    ).toBe(true);
  });
  it.each([0, 49, 50, 51])(
    "lists %s unrevoked keys before a bounded recent revoked history",
    async (count) => {
      const f = await machineFixture(modules);
      const first = await firstKey(f);
      const { current, revoked } = await f.t.run(async (ctx) => {
        await ctx.db.delete(first._id);
        const current: Id<"machine_api_keys">[] = [];
        const revoked: Id<"machine_api_keys">[] = [];
        for (let n = 0; n < count + 55; n++) {
          const id = await ctx.db.insert("machine_api_keys", {
            machineId: f.machineId,
            selector: n.toString(16).padStart(32, "0"),
            keyHash: "synthetic-verifier",
            prefix: `synthetic-${n}`,
            createdAt: NOW,
            revokedAt: n < count ? undefined : NOW + n,
          });
          (n < count ? current : revoked).unshift(id);
        }
        return { current, revoked };
      });

      const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
        machineId: f.machineId,
      });

      expect(
        listed.map((key) => (key.kind === "table" ? key._id : "legacy")),
      ).toEqual([
        ...current,
        ...revoked.slice(0, Math.max(0, LISTED_KEYS - count)),
      ]);
    },
  );

  it("lists unrevoked keys first, with their state, and no secret", async () => {
    const logs = [
      vi.spyOn(console, "log"),
      vi.spyOn(console, "warn"),
      vi.spyOn(console, "error"),
    ];
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await status(f, f.apiKey);
    at(NOW + 1_000);
    const replacing = await rotate(f);
    at(NOW + 2_000);
    const revoked = await rotate(f, { lifetimeMinutes: 30 * 24 * 60 });
    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: revoked.keyId,
    });

    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: f.machineId,
    });

    expect(listed).toEqual([
      {
        kind: "table",
        _id: replacing.keyId,
        prefix: replacing.prefix,
        state: "replaced",
        createdAt: NOW + 1_000,
        createdByName: "Synthetic Admin",
        lastUsedAt: null,
        expiresAt: NOW + 2_000 + OVERLAP,
        revokedAt: null,
      },
      {
        kind: "table",
        _id: old._id,
        prefix: old.prefix,
        state: "replaced",
        createdAt: NOW,
        createdByName: "Synthetic Admin",
        lastUsedAt: NOW,
        expiresAt: NOW + 1_000 + OVERLAP,
        revokedAt: null,
      },
      {
        kind: "table",
        _id: revoked.keyId,
        prefix: revoked.prefix,
        state: "revoked",
        createdAt: NOW + 2_000,
        createdByName: "Synthetic Admin",
        lastUsedAt: null,
        expiresAt: NOW + 2_000 + 30 * DAY,
        revokedAt: NOW + 2_000,
      },
    ]);
    const keys = await keysOf(f);
    for (const output of [listed, logs.map((spy) => spy.mock.calls)]) {
      const text = JSON.stringify(output);
      for (const plain of [f.apiKey, replacing.apiKey, revoked.apiKey]) {
        expect(text.includes(plain)).toBe(false);
      }
      for (const key of keys) {
        expect(text.includes(key.keyHash)).toBe(false);
        expect(text.includes(key.selector)).toBe(false);
      }
    }

    at(NOW + 1_000 + OVERLAP);
    const later = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: f.machineId,
    });
    // Revoking the key that replaced another does not lengthen the other:
    // it stays "replaced" until the end of its own overlap.
    expect(later.map((key) => key.state)).toEqual([
      "replaced",
      "expired",
      "revoked",
    ]);
  });

  it("lists 50 keys at most", async () => {
    const f = await machineFixture(modules);
    for (let n = 0; n < LISTED_KEYS; n++) {
      await f.t.mutation(internal.machineKeys.createKeyByOperator, {
        machineId: f.machineId,
        overlapMinutes: 0,
      });
    }

    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: f.machineId,
    });

    expect(listed).toHaveLength(LISTED_KEYS);
    expect(listed.map((key) => key.state).slice(0, 2)).toEqual([
      "active",
      "revoked",
    ]);
  });

  it("lists a deleted machine's keys to the Anheart admin only", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.insert("machine_api_keys", {
        machineId: w.machine,
        selector: "ef".repeat(16),
        keyHash: "synthetic-verifier",
        prefix: "anh1.efefefef",
        createdAt: NOW,
      });
      await ctx.db.patch(w.machine, { isDeleted: true });
    });
    const list = (actor: "admin" | "orgAdmin") =>
      as(w.t, actor).query(api.machineKeys.listMachineKeys, {
        machineId: w.machine,
      });

    expect((await list("admin")).map((key) => key.prefix)).toEqual([
      "anh1.efefefef",
    ]);
    expect(await list("orgAdmin")).toEqual([]);
    await expect(
      as(w.t, "orgAdmin").mutation(api.machineKeys.createMachineKey, {
        machineId: w.machine,
      }),
    ).rejects.toThrow(/machine_not_found/);
  });

  it("answers [] for a machine that does not exist", async () => {
    const f = await machineFixture(modules);
    await f.t.run((ctx) => ctx.db.delete(f.machineId));

    expect(
      await f.admin.query(api.machineKeys.listMachineKeys, {
        machineId: f.machineId,
      }),
    ).toEqual([]);
  });
});

describe("ANH-165 EX-2 the keys from the command line", () => {
  it("creates a key without author, under the same rotation rules", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    const issued = await f.t.mutation(
      internal.machineKeys.createKeyByOperator,
      { machineId: f.machineId, overlapMinutes: 3 },
    );

    const key = await keyOf(f, issued.keyId);
    expect(key.createdBy).toBeUndefined();
    expect((await keyOf(f, old._id)).expiresAt).toBe(NOW + 3 * 60_000);
    expect(await status(f, issued.apiKey)).toBe(200);
    const [listed] = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: f.machineId,
    });
    expect(listed.kind === "table" && listed.createdByName).toBeNull();
  });

  it("revokes a key at once, and leaves a revoked key as it was", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);

    await f.t.mutation(internal.machineKeys.revokeKeyByOperator, {
      keyId: old._id,
    });
    at(NOW + HOUR);
    await f.t.mutation(internal.machineKeys.revokeKeyByOperator, {
      keyId: old._id,
    });

    const key = await keyOf(f, old._id);
    expect(key.revokedAt).toBe(NOW);
    expect(key.revokedBy).toBeUndefined();
    expect(await status(f, f.apiKey)).toBe(401);
  });

  it("refuses a deleted machine and a key that does not exist", async () => {
    const f = await machineFixture(modules);
    const old = await firstKey(f);
    await f.admin.mutation(api.machines.deleteMachine, {
      machineId: f.machineId,
    });
    await expect(
      f.t.mutation(internal.machineKeys.createKeyByOperator, {
        machineId: f.machineId,
      }),
    ).rejects.toThrow(/machine_not_found/);

    await f.t.run((ctx) => ctx.db.delete(old._id));
    await expect(
      f.t.mutation(internal.machineKeys.revokeKeyByOperator, {
        keyId: old._id,
      }),
    ).rejects.toThrow(/machine_key_not_found/);
  });
});

describe("ANH-165 EX-2 the key of a machine written before is moved, never refused", () => {
  /** A machine as ANH-121 wrote it: its single key on the machine itself. */
  async function legacyMachine(f: Fixture) {
    const { plain, hashed, selector } = await generateApiKey();
    const organizationId = (await f.t.run((ctx) => ctx.db.get(f.machineId)))
      ?.organizationId;
    const machineId = await f.t.run((ctx) =>
      ctx.db.insert("machines", {
        organizationId,
        name: "Machine of ANH-121",
        apiKey: hashed,
        apiKeySelector: selector,
        status: "offline" as const,
        lastHeartbeat: 0,
        createdAt: NOW - DAY,
      }),
    );
    return { machineId, apiKey: plain, selector };
  }

  it("lists the historical key before migration without exposing its selector or verifier", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const machine = await f.t.run((ctx) => ctx.db.get(legacy.machineId));

    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: legacy.machineId,
    });

    expect(listed).toEqual([
      { kind: "legacy", prefix: keyPrefix(legacy.selector), state: "active" },
    ]);
    const text = JSON.stringify(listed);
    expect(text.includes(legacy.selector)).toBe(false);
    expect(text.includes(machine?.apiKey ?? "missing verifier")).toBe(false);
    expect(text.includes(legacy.apiKey)).toBe(false);
  });

  it("keeps the historical key first and counts it before filling the revoked history", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const tableIds = await f.t.run(async (ctx) => {
      const ids: Id<"machine_api_keys">[] = [];
      for (let n = 0; n < 51; n++)
        ids.unshift(
          await ctx.db.insert("machine_api_keys", {
            machineId: legacy.machineId,
            selector: `synthetic-${n}`,
            keyHash: "synthetic-verifier",
            prefix: `synthetic-${n}`,
            createdAt: NOW,
            revokedAt: n === 0 ? undefined : NOW + n,
          }),
        );
      return ids;
    });

    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: legacy.machineId,
    });

    expect(listed).toHaveLength(50);
    expect(listed[0]).toEqual({
      kind: "legacy",
      prefix: keyPrefix(legacy.selector),
      state: "active",
    });
    expect(
      listed.slice(1).map((key) => (key.kind === "table" ? key._id : "legacy")),
    ).toEqual([tableIds[50], ...tableIds.slice(0, 48)]);
  });

  it("revokes the historical key on all 13 routes, audits it, and preserves another machine", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const other = await f.t.run((ctx) => ctx.db.get(f.machineId));
    const otherKeys = await keysOf(f);
    const beforeAudit = await f.t.run((ctx) =>
      ctx.db.query("audit_log").collect(),
    );
    expect(await status(f, legacy.apiKey)).toBe(200);

    await f.admin.mutation(api.machineKeys.revokeLegacyMachineKey, {
      machineId: legacy.machineId,
    });

    expect(machineRoutes).toHaveLength(13);
    for (const [method, path] of machineRoutes) {
      expect(
        (
          await f.t.fetch(path, {
            method,
            headers: machineHeaders(legacy.apiKey),
          })
        ).status,
        `${method} ${path}`,
      ).toBe(401);
    }
    const [revoked] = await keysOf(f, legacy.machineId);
    expect(revoked).toMatchObject({
      revokedAt: NOW,
      revokedBy: otherKeys[0].createdBy,
      prefix: keyPrefix(legacy.selector),
    });
    const audits = await f.t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(audits.slice(beforeAudit.length)).toMatchObject([
      {
        action: "machine_key.revoked",
        targetId: revoked._id,
        details: { machineId: legacy.machineId },
      },
    ]);
    expect(await f.t.run((ctx) => ctx.db.get(f.machineId))).toEqual(other);
    expect(await keysOf(f)).toEqual(otherKeys);
    expect(await status(f, f.apiKey)).toBe(200);
  });

  it("revokes a deleted machine's historical key before restoration can reopen it", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    await f.admin.mutation(api.machines.deleteMachine, {
      machineId: legacy.machineId,
    });

    await f.admin.mutation(api.machineKeys.revokeLegacyMachineKey, {
      machineId: legacy.machineId,
    });
    await f.admin.mutation(api.machines.restoreMachine, {
      machineId: legacy.machineId,
    });

    expect((await keysOf(f, legacy.machineId))[0].revokedAt).toBe(NOW);
    expect(await status(f, legacy.apiKey)).toBe(401);
  });

  it.each([
    "missing machine",
    "no fields",
    "missing verifier",
    "missing selector",
    "bad selector",
    "reversible",
    "same selector",
    "foreign selector",
  ])(
    "refuses a historical revocation and writes nothing for %s",
    async (scenario) => {
      const f = await machineFixture(modules);
      const legacy = await legacyMachine(f);
      await f.t.run(async (ctx) => {
        switch (scenario) {
          case "missing machine":
            await ctx.db.delete(legacy.machineId);
            break;
          case "no fields":
            await ctx.db.patch(legacy.machineId, {
              apiKey: undefined,
              apiKeySelector: undefined,
            });
            break;
          case "missing verifier":
            await ctx.db.patch(legacy.machineId, { apiKey: undefined });
            break;
          case "missing selector":
            await ctx.db.patch(legacy.machineId, { apiKeySelector: undefined });
            break;
          case "bad selector":
            await ctx.db.patch(legacy.machineId, { apiKeySelector: "invalid" });
            break;
          case "reversible":
            await ctx.db.patch(legacy.machineId, { apiKey: "b64:reversible" });
            break;
          case "same selector":
            await ctx.db.insert("machine_api_keys", {
              machineId: legacy.machineId,
              selector: legacy.selector,
              keyHash: "synthetic-verifier",
              prefix: keyPrefix(legacy.selector),
              createdAt: NOW,
              revokedAt: NOW,
            });
            break;
          case "foreign selector":
            await ctx.db.patch(legacy.machineId, {
              apiKeySelector: (await ctx.db.query("machine_api_keys").first())
                ?.selector,
            });
            break;
        }
      });
      const snapshot = () =>
        f.t.run(async (ctx) => ({
          machines: await ctx.db.query("machines").collect(),
          keys: await ctx.db.query("machine_api_keys").collect(),
          audit: await ctx.db.query("audit_log").collect(),
        }));
      const before = await snapshot();

      await expect(
        f.admin.mutation(api.machineKeys.revokeLegacyMachineKey, {
          machineId: legacy.machineId,
        }),
      ).rejects.toThrow(/machine_key_not_found/);

      expect(await snapshot()).toEqual(before);
      const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
        machineId: legacy.machineId,
      });
      expect(listed.some((key) => key.kind === "legacy")).toBe(false);
    },
  );

  it("goes on opening the routes before the migration, with no use written", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);

    expect(await status(f, legacy.apiKey)).toBe(200);
    const found = await f.t.query(internal.machines.getMachineByApiKey, {
      apiKey: legacy.apiKey,
    });
    expect(found).toEqual({
      _id: legacy.machineId,
      name: "Machine of ANH-121",
      status: "offline",
    });
    const wrong =
      legacy.apiKey.slice(0, -1) + (legacy.apiKey.endsWith("0") ? "1" : "0");
    expect(await status(f, wrong)).toBe(401);
  });

  it("moves the key into the table, where the same key goes on opening the routes", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const verifier = (await f.t.run((ctx) => ctx.db.get(legacy.machineId)))
      ?.apiKey;

    at(NOW + HOUR);
    const result = await f.t.action(
      internal.migrations.machineApiKeys.moveMachineKeysToTable,
      {},
    );

    expect(result).toEqual({
      machinesChecked: 2,
      keysMoved: 1,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    });
    const machine = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
    expect(machine?.apiKey).toBeUndefined();
    expect(machine?.apiKeySelector).toBeUndefined();
    const [moved, ...others] = await keysOf(f, legacy.machineId);
    expect(others).toEqual([]);
    expect(moved).toMatchObject({
      selector: legacy.selector,
      keyHash: verifier,
      prefix: keyPrefix(legacy.selector),
      createdAt: NOW + HOUR,
    });
    expect(moved.createdBy).toBeUndefined();
    const listed = await f.admin.query(api.machineKeys.listMachineKeys, {
      machineId: legacy.machineId,
    });
    expect(listed).toMatchObject([
      { kind: "table", _id: moved._id, prefix: keyPrefix(legacy.selector) },
    ]);
    expect(await status(f, legacy.apiKey)).toBe(200);
    expect((await keyOf(f, moved._id)).lastUsedAt).toBe(NOW + HOUR);
  });

  it("finds nothing left to move on a second run", async () => {
    const f = await machineFixture(modules);
    await legacyMachine(f);
    const move = () =>
      f.t.action(internal.migrations.machineApiKeys.moveMachineKeysToTable, {});
    await move();
    const rows = await f.t.run((ctx) =>
      ctx.db.query("machine_api_keys").collect(),
    );

    expect(await move()).toEqual({
      machinesChecked: 2,
      keysMoved: 0,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    });
    expect(
      await f.t.run((ctx) => ctx.db.query("machine_api_keys").collect()),
    ).toEqual(rows);
  });

  it("refuses a moved key once it is revoked: the machine's old fields are no way back", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    await f.t.action(
      internal.migrations.machineApiKeys.moveMachineKeysToTable,
      {},
    );
    const [moved] = await keysOf(f, legacy.machineId);

    await f.admin.mutation(api.machineKeys.revokeMachineKey, {
      keyId: moved._id,
    });

    expect(await status(f, legacy.apiKey)).toBe(401);
  });

  it("decides by the table alone once a selector is there, even if the machine still carries it", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const verifier = (await f.t.run((ctx) => ctx.db.get(legacy.machineId)))
      ?.apiKey as string;
    // A row of the same key, revoked, next to the legacy fields.
    await f.t.run((ctx) =>
      ctx.db.insert("machine_api_keys", {
        machineId: legacy.machineId,
        selector: legacy.selector,
        keyHash: verifier,
        prefix: keyPrefix(legacy.selector),
        createdAt: NOW,
        revokedAt: NOW,
      }),
    );

    expect(await status(f, legacy.apiKey)).toBe(401);
    // The migration then clears the fields without adding a second row.
    expect(
      await f.t.action(
        internal.migrations.machineApiKeys.moveMachineKeysToTable,
        {},
      ),
    ).toEqual({
      machinesChecked: 2,
      keysMoved: 1,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    });
    expect(await keysOf(f, legacy.machineId)).toHaveLength(1);
    const cleared = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
    expect(cleared?.apiKey).toBeUndefined();
    expect(cleared?.apiKeySelector).toBeUndefined();
  });

  it("counts a selector owned by another machine as a conflict and leaves both machines' keys intact", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const before = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
    const owner = await firstKey(f);
    await f.t.run((ctx) =>
      ctx.db.patch(legacy.machineId, { apiKeySelector: owner.selector }),
    );
    const move = () =>
      f.t.action(internal.migrations.machineApiKeys.moveMachineKeysToTable, {});
    const rows = await f.t.run((ctx) =>
      ctx.db.query("machine_api_keys").collect(),
    );

    for (let run = 0; run < 2; run++) {
      expect(await move()).toEqual({
        machinesChecked: 2,
        keysMoved: 0,
        legacyDiscarded: 0,
        selectorConflicts: 1,
      });
      expect(
        await f.t.run((ctx) => ctx.db.query("machine_api_keys").collect()),
      ).toEqual(rows);
      const machine = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
      expect(machine?.apiKey).toBe(before?.apiKey);
      expect(machine?.apiKeySelector).toBe(owner.selector);
    }
    const conflictingKey = legacy.apiKey.replace(
      legacy.selector,
      owner.selector,
    );
    for (const apiKey of [legacy.apiKey, conflictingKey]) {
      for (const [method, route] of machineRoutes) {
        expect(
          (await f.t.fetch(route, { method, headers: machineHeaders(apiKey) }))
            .status,
        ).toBe(401);
      }
    }
    expect(await status(f, f.apiKey)).toBe(200);
    expect(
      (
        await f.t.query(internal.machines.getMachineByApiKey, {
          apiKey: f.apiKey,
        })
      )?._id,
    ).toBe(f.machineId);
    expect(await keysOf(f, legacy.machineId)).toEqual([]);
  });

  it("issues a new key on rotation despite a selector conflict, without clearing the legacy fields", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);
    const owner = await firstKey(f);
    await f.t.run((ctx) =>
      ctx.db.patch(legacy.machineId, { apiKeySelector: owner.selector }),
    );
    const before = await f.t.run((ctx) => ctx.db.get(legacy.machineId));

    const issued = await f.admin.mutation(api.machineKeys.createMachineKey, {
      machineId: legacy.machineId,
    });

    expect(await status(f, issued.apiKey)).toBe(200);
    expect(await status(f, f.apiKey)).toBe(200);
    expect(await keysOf(f, legacy.machineId)).toHaveLength(1);
    const machine = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
    expect(machine?.apiKey).toBe(before?.apiKey);
    expect(machine?.apiKeySelector).toBe(owner.selector);
    expect((await keyOf(f, owner._id)).replacedBy).toBeUndefined();
    expect((await keyOf(f, owner._id)).revokedAt).toBeUndefined();
  });

  it("clears a reversible legacy value instead of moving it", async () => {
    const f = await machineFixture(modules);
    await f.t.run((ctx) =>
      ctx.db.patch(f.machineId, {
        apiKey: btoa("synthetic-legacy").split("").reverse().join(""),
      }),
    );
    const selectorOnly = await f.t.run((ctx) =>
      ctx.db.insert("machines", {
        name: "Selector without verifier",
        apiKeySelector: "12".repeat(16),
        status: "offline" as const,
        lastHeartbeat: 0,
        createdAt: NOW,
      }),
    );

    expect(
      await f.t.action(
        internal.migrations.machineApiKeys.moveMachineKeysToTable,
        {},
      ),
    ).toEqual({
      machinesChecked: 2,
      keysMoved: 0,
      legacyDiscarded: 2,
      selectorConflicts: 0,
    });
    for (const id of [f.machineId, selectorOnly]) {
      const machine = await f.t.run((ctx) => ctx.db.get(id));
      expect(machine?.apiKey).toBeUndefined();
      expect(machine?.apiKeySelector).toBeUndefined();
    }
    expect(await keysOf(f)).toHaveLength(1);
    expect(await status(f, f.apiKey)).toBe(200);
  });

  it.each(["malformed selector", "invalid verifier"])(
    "discards a legacy key with %s instead of inserting it",
    async (kind) => {
      const f = await machineFixture(modules);
      const legacy = await legacyMachine(f);
      await f.t.run((ctx) =>
        ctx.db.patch(
          legacy.machineId,
          kind === "malformed selector"
            ? { apiKeySelector: "not-a-selector" }
            : { apiKey: btoa("synthetic-legacy") },
        ),
      );

      expect(
        await f.t.action(
          internal.migrations.machineApiKeys.moveMachineKeysToTable,
          {},
        ),
      ).toEqual({
        machinesChecked: 2,
        keysMoved: 0,
        legacyDiscarded: 1,
        selectorConflicts: 0,
      });
      const machine = await f.t.run((ctx) => ctx.db.get(legacy.machineId));
      expect(machine?.apiKey).toBeUndefined();
      expect(machine?.apiKeySelector).toBeUndefined();
      expect(await keysOf(f, legacy.machineId)).toEqual([]);
    },
  );

  it.each([{}, { pageSize: 1 }, { pageSize: 2 }, { pageSize: 1000 }])(
    "sums every machine exactly once across mixed pages (%j)",
    async (args) => {
      const f = await machineFixture(modules);
      const moved = await legacyMachine(f);
      const conflict = await legacyMachine(f);
      const owner = await firstKey(f);
      await f.t.run(async (ctx) => {
        await ctx.db.patch(conflict.machineId, {
          apiKeySelector: owner.selector,
        });
        await ctx.db.insert("machines", {
          name: "Reversible legacy value",
          apiKey: btoa("synthetic-legacy"),
          status: "offline",
          lastHeartbeat: 0,
          createdAt: NOW,
        });
        await ctx.db.insert("machines", {
          name: "No key",
          status: "offline",
          lastHeartbeat: 0,
          createdAt: NOW,
        });
      });
      const before = await f.t.run((ctx) => ctx.db.query("machines").collect());

      expect(
        await f.t.action(
          internal.migrations.machineApiKeys.moveMachineKeysToTable,
          args,
        ),
      ).toEqual({
        machinesChecked: before.length,
        keysMoved: 1,
        legacyDiscarded: 1,
        selectorConflicts: 1,
      });
      expect(before).toHaveLength(5);
      for (const machine of await f.t.run((ctx) =>
        ctx.db.query("machines").collect(),
      )) {
        const original = before.find((row) => row._id === machine._id);
        if (machine._id === conflict.machineId) {
          expect(machine).toEqual(original);
        } else {
          expect(machine).toEqual({
            ...original,
            apiKey: undefined,
            apiKeySelector: undefined,
          });
        }
      }
      expect(await keysOf(f, moved.machineId)).toHaveLength(1);
      expect(await keysOf(f, conflict.machineId)).toHaveLength(0);
      expect(
        await f.t.run((ctx) => ctx.db.query("machine_api_keys").collect()),
      ).toHaveLength(2);
      expect(await status(f, moved.apiKey)).toBe(200);
      expect(
        await f.t.action(
          internal.migrations.machineApiKeys.moveMachineKeysToTable,
          args,
        ),
      ).toEqual({
        machinesChecked: 5,
        keysMoved: 0,
        legacyDiscarded: 0,
        selectorConflicts: 1,
      });
    },
  );

  it.each([0, 1001, 1.5, Number.NaN, Number.POSITIVE_INFINITY])(
    "refuses pageSize %s before writing anything",
    async (pageSize) => {
      const f = await machineFixture(modules);
      await legacyMachine(f);
      const snapshot = () =>
        f.t.run(async (ctx) => ({
          machines: await ctx.db.query("machines").collect(),
          keys: await ctx.db.query("machine_api_keys").collect(),
        }));
      const before = await snapshot();

      await expect(
        f.t.action(internal.migrations.machineApiKeys.moveMachineKeysToTable, {
          pageSize,
        }),
      ).rejects.toThrow(
        "Machine key migration pageSize must be an integer from 1 to 1000.",
      );

      expect(await snapshot()).toEqual(before);
    },
  );

  it("returns zero counts for an empty fleet", async () => {
    const t = backendOf(modules);
    expect(
      await t.action(
        internal.migrations.machineApiKeys.moveMachineKeysToTable,
        {},
      ),
    ).toEqual({
      machinesChecked: 0,
      keysMoved: 0,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    });
  });

  it("keeps a committed page after interruption and resumes by rerunning the command", async () => {
    const f = await machineFixture(modules);
    const first = await legacyMachine(f);
    const second = await legacyMachine(f);

    const page = await f.t.mutation(
      internal.migrations.machineApiKeys.moveMachineKeysPage,
      {
        paginationOpts: { cursor: null, numItems: 2 },
      },
    );

    expect(page).toMatchObject({
      machinesChecked: 2,
      keysMoved: 1,
      legacyDiscarded: 0,
      selectorConflicts: 0,
      isDone: false,
    });
    expect(await keysOf(f, first.machineId)).toHaveLength(1);
    expect(await keysOf(f, second.machineId)).toHaveLength(0);
    expect(
      await f.t.action(
        internal.migrations.machineApiKeys.moveMachineKeysToTable,
        { pageSize: 2 },
      ),
    ).toEqual({
      machinesChecked: 3,
      keysMoved: 1,
      legacyDiscarded: 0,
      selectorConflicts: 0,
    });
    expect(await keysOf(f, first.machineId)).toHaveLength(1);
    expect(await keysOf(f, second.machineId)).toHaveLength(1);
  });

  it("brings a key not moved yet under the rotation: it keeps its overlap, then is refused", async () => {
    const f = await machineFixture(modules);
    const legacy = await legacyMachine(f);

    const issued = await f.admin.mutation(api.machineKeys.createMachineKey, {
      machineId: legacy.machineId,
    });

    const keys = await keysOf(f, legacy.machineId);
    expect(keys.map((key) => key.replacedBy ?? null)).toEqual([
      issued.keyId,
      null,
    ]);
    expect(await status(f, legacy.apiKey)).toBe(200);
    expect(await status(f, issued.apiKey)).toBe(200);
    at(NOW + OVERLAP);
    expect(await status(f, legacy.apiKey)).toBe(401);
    expect(await status(f, issued.apiKey)).toBe(200);
  });
});
