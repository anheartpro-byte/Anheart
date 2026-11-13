/// <reference types="vite/client" />
/**
 * ANH-152: who may act at a machine's console. The operators an admin enables
 * and disables, the code an operator sets for themselves (hashed with scrypt,
 * never readable), the tablets, and the signed list a machine fetches
 * (`GET /api/machine/operators`, `operator_list` of contracts/machine-api.json).
 * Who may call each function, cell by cell, is `authorization.matrix.ts`.
 *
 * Every key and code here is made at run time: no secret is written anywhere.
 */
import { scryptSync } from "node:crypto";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { CATALOG_SIGNING_KEY_ENV } from "./lib/programmes";
import {
  isAcceptableCode,
  MAX_DEVICES,
  MAX_OPERATORS,
  OPERATOR_CODE,
  operatorListPayload,
  tokenSha256,
} from "./lib/consoleAccess";
import { as, modules, NOW, seedWorld, type World } from "./test.setup";
import machineApi from "../contracts/machine-api.json";

afterEach(() => {
  vi.unstubAllEnvs();
});

const base64 = (bytes: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const unbase64 = (text: string) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));

/** A code nobody would guess and the rules accept, different at each call. */
function aCode(): string {
  for (;;) {
    const code = Array.from(crypto.getRandomValues(new Uint8Array(6)), (b) => b % 10).join("");
    if (isAcceptableCode(code)) return code;
  }
}

async function keyPair() {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  return {
    privateBase64: base64(await crypto.subtle.exportKey("pkcs8", pair.privateKey)),
    publicKey: pair.publicKey,
  };
}

const enable = (w: World, userId: Id<"users">, machineId = w.machine) =>
  as(w.t, "orgAdmin").mutation(api.consoleAccess.enableOperator, { machineId, userId });

const rowsOf = (w: World, userId: Id<"users">) =>
  w.t.run((ctx) =>
    ctx.db
      .query("machine_operators")
      .withIndex("by_user", (q) => q.eq("userId", userId))
      .collect(),
  );

type Payload = {
  kind: string;
  machineId: string;
  issuedAt: number;
  operators: Array<{ userId: string; name: string; code: Record<string, unknown> }>;
  devices: Array<{ deviceId: string; name: string; tokenSha256: string }>;
};

async function fetchList(w: World, key: string) {
  const response = await w.t.fetch("/api/machine/operators", {
    method: "GET",
    headers: { Authorization: `Bearer ${key}`, [CONTRACT_HEADER]: CONTRACT_VERSION },
  });
  expect(response.status).toBe(200);
  return (await response.json()) as {
    status: string;
    payload?: string;
    signature?: string;
    server_contract_version: string;
  };
}

async function machineKey(w: World) {
  return (
    await as(w.t, "admin").mutation(api.machineKeys.createMachineKey, { machineId: w.machine })
  ).apiKey;
}

describe("ANH-152 EX-1 the form of a console code", () => {
  it.each(["482915", "20261113", "839201746512"])("accepts %s", (code) => {
    expect(isAcceptableCode(code)).toBe(true);
  });

  it.each([
    ["five digits", "48291"],
    ["thirteen digits", "4829150000017"],
    ["a letter", "48a915"],
    ["a space", " 482915"],
    ["one digit repeated", "000000"],
    ["a run up", "123456"],
    ["a run down", "987654"],
    ["a run up through 0", "890123"],
  ])("refuses %s", (_why, code) => {
    expect(isAcceptableCode(code)).toBe(false);
  });
});

describe("ANH-152 EX-1 the operator sets their own code, hashed", () => {
  it("keeps only the scrypt hash, with its parameters and its salt, on every row of theirs", async () => {
    const w = await seedWorld(modules);
    await enable(w, w.manager);
    await enable(w, w.manager, w.otherMachine);
    const code = aCode();
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code });
    const rows = await rowsOf(w, w.manager);
    expect(rows).toHaveLength(2);
    for (const row of rows) {
      const stored = row.operatorCode;
      if (stored === undefined) throw new Error("no code stored");
      expect(stored).toMatchObject({
        algorithm: "scrypt",
        n: OPERATOR_CODE.n,
        r: OPERATOR_CODE.r,
        p: OPERATOR_CODE.p,
      });
      expect(unbase64(stored.salt)).toHaveLength(OPERATOR_CODE.salt_bytes);
      const again = scryptSync(code, unbase64(stored.salt), OPERATOR_CODE.hash_bytes, {
        N: stored.n,
        r: stored.r,
        p: stored.p,
        maxmem: 256 * stored.n * stored.r,
      });
      expect(again.toString("base64")).toBe(stored.hash);
      expect(JSON.stringify(row)).not.toContain(code);
      expect(row.codeSetAt).toBeTypeOf("number");
    }
    const audit = await w.t.run((ctx) =>
      ctx.db
        .query("audit_log")
        .filter((q) => q.eq(q.field("action"), "machine_operator.code_set"))
        .collect(),
    );
    expect(audit).toHaveLength(1);
    expect(JSON.stringify(audit)).not.toContain(code);
  });

  it("refuses a code of the wrong form and writes nothing", async () => {
    const w = await seedWorld(modules);
    await enable(w, w.manager);
    await expect(
      as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: "123456" }),
    ).rejects.toThrow(/invalid_operator_code/);
    expect((await rowsOf(w, w.manager))[0].operatorCode).toBeUndefined();
  });

  it("refuses a member enabled on no machine, before anything is hashed", async () => {
    const w = await seedWorld(modules);
    await expect(
      as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() }),
    ).rejects.toThrow(/operator_not_enabled/);
    await enable(w, w.manager);
    await as(w.t, "orgAdmin").mutation(api.consoleAccess.disableOperator, {
      machineId: w.machine,
      userId: w.manager,
    });
    await expect(
      as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() }),
    ).rejects.toThrow(/operator_not_enabled/);
  });

  it("gives a machine enabled later the code already set, and a new code replaces it", async () => {
    const w = await seedWorld(modules);
    await enable(w, w.manager);
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    await enable(w, w.manager, w.otherMachine);
    const [first, second] = await rowsOf(w, w.manager);
    expect(second.operatorCode).toEqual(first.operatorCode);
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    const [renewed] = await rowsOf(w, w.manager);
    expect(renewed.operatorCode?.hash).not.toBe(first.operatorCode?.hash);
    expect(renewed.operatorCode?.salt).not.toBe(first.operatorCode?.salt);
  });
});

describe("ANH-152 EX-1 enabling and disabling an operator", () => {
  it("enables only an active admin or gestionnaire of the machine's organisation", async () => {
    const w = await seedWorld(modules);
    await expect(enable(w, w.patient)).rejects.toThrow(/operator_not_eligible/);
    await expect(enable(w, w.orgBManager)).rejects.toThrow(/operator_not_eligible/);
    await enable(w, w.orgAdmin);
    await enable(w, w.otherManager);
    expect(await rowsOf(w, w.patient)).toHaveLength(0);
    expect(await rowsOf(w, w.orgAdmin)).toHaveLength(1);
  });

  it("changes nothing when enabling twice or disabling twice, and re-enables a row", async () => {
    const w = await seedWorld(modules);
    await enable(w, w.manager);
    await enable(w, w.manager);
    const disable = () =>
      as(w.t, "orgAdmin").mutation(api.consoleAccess.disableOperator, {
        machineId: w.machine,
        userId: w.manager,
      });
    await disable();
    await disable();
    const [disabled] = await rowsOf(w, w.manager);
    expect(disabled.disabledAt).toBeTypeOf("number");
    await enable(w, w.manager);
    const rows = await rowsOf(w, w.manager);
    expect(rows).toHaveLength(1);
    expect(rows[0].disabledAt).toBeUndefined();
    const actions = await w.t.run((ctx) =>
      ctx.db
        .query("audit_log")
        .filter((q) => q.eq(q.field("targetType"), "machine_operator"))
        .collect(),
    );
    expect(actions.map((a) => a.action)).toEqual([
      "machine_operator.enabled",
      "machine_operator.disabled",
      "machine_operator.enabled",
    ]);
  });

  it("refuses to disable an account that is not an operator of the machine", async () => {
    const w = await seedWorld(modules);
    await expect(
      as(w.t, "orgAdmin").mutation(api.consoleAccess.disableOperator, {
        machineId: w.machine,
        userId: w.manager,
      }),
    ).rejects.toThrow(/machine_operator_not_found/);
  });

  it("refuses an operator past what a console reads, and a deleted machine", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let n = 0; n < MAX_OPERATORS; n += 1) {
        await ctx.db.insert("machine_operators", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId: w.otherManager,
          enabledAt: NOW,
          enabledBy: w.orgAdmin,
        });
      }
    });
    await expect(enable(w, w.manager)).rejects.toThrow(/too_many_operators/);
    await w.t.run((ctx) => ctx.db.patch(w.otherMachine, { isDeleted: true }));
    await expect(enable(w, w.manager, w.otherMachine)).rejects.toThrow(/machine_not_found/);
  });

  it("lists operators, candidates and tablets to an admin, never a code nor a token", async () => {
    const w = await seedWorld(modules);
    await enable(w, w.manager);
    const code = aCode();
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code });
    const { token } = await as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, {
      machineId: w.machine,
      name: "Tablette du banc",
    });
    const listed = await as(w.t, "orgAdmin").query(api.consoleAccess.listConsoleAccess, {
      machineId: w.machine,
    });
    expect(listed?.operators).toEqual([
      expect.objectContaining({
        userId: w.manager,
        name: "manager Synthetic",
        enabled: true,
        eligible: true,
        codeSet: true,
      }),
    ]);
    expect(listed?.candidates.map((c) => c.userId).sort()).toEqual(
      [w.orgAdmin, w.otherManager].sort(),
    );
    expect(listed?.devices).toEqual([
      expect.objectContaining({ name: "Tablette du banc", prefix: token.slice(0, 14), revokedAt: null }),
    ]);
    const text = JSON.stringify(listed);
    expect(text).not.toContain(code);
    expect(text).not.toContain(token);
    expect(text).not.toContain("scrypt");
    const mine = await as(w.t, "manager").query(api.consoleAccess.listMyOperatorMachines, {});
    expect(mine).toEqual([
      expect.objectContaining({ machineId: w.machine, codeSet: true }),
    ]);
    expect(JSON.stringify(mine)).not.toContain("scrypt");
  });
});

describe("ANH-152 EX-3 tablets", () => {
  it("answers the token once, keeps its SHA-256 only, refuses a bad name", async () => {
    const w = await seedWorld(modules);
    const issued = await as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, {
      machineId: w.machine,
      name: "  Tablette 1  ",
    });
    expect(issued.token).toMatch(/^anhd1\.[0-9a-f]{64}$/);
    const device = await w.t.run((ctx) => ctx.db.get(issued.deviceId));
    expect(device?.name).toBe("Tablette 1");
    expect(device?.tokenSha256).toBe(await tokenSha256(issued.token));
    expect(JSON.stringify(device)).not.toContain(issued.token);
    for (const name of ["   ", "x".repeat(101)]) {
      await expect(
        as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, { machineId: w.machine, name }),
      ).rejects.toThrow(/invalid_device_name/);
    }
  });

  it("revokes once, and refuses a tablet past what a console reads", async () => {
    const w = await seedWorld(modules);
    const { deviceId } = await as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, {
      machineId: w.machine,
      name: "T",
    });
    const revoke = () => as(w.t, "orgAdmin").mutation(api.consoleAccess.revokeDevice, { deviceId });
    await revoke();
    const revokedAt = (await w.t.run((ctx) => ctx.db.get(deviceId)))?.revokedAt;
    expect(revokedAt).toBeTypeOf("number");
    await revoke();
    expect((await w.t.run((ctx) => ctx.db.get(deviceId)))?.revokedAt).toBe(revokedAt);
    for (let n = 0; n < MAX_DEVICES; n += 1) {
      await as(w.t, "admin").mutation(api.consoleAccess.createDevice, { machineId: w.machine, name: `T${n}` });
    }
    await expect(
      as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, { machineId: w.machine, name: "one more" }),
    ).rejects.toThrow(/too_many_devices/);
  });
});

describe("ANH-152 EX-1 GET /api/machine/operators", () => {
  it("answers not_configured while the signing key is absent", async () => {
    const w = await seedWorld(modules);
    expect(await fetchList(w, await machineKey(w))).toEqual({
      status: "not_configured",
      server_contract_version: CONTRACT_VERSION,
    });
  });

  it("signs the machine's enabled operators with a code and its unrevoked tablets", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const keys = await keyPair();
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, keys.privateBase64);
    await enable(w, w.manager);
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    await enable(w, w.otherManager); // no code yet: not listed
    await enable(w, w.orgAdmin);
    await as(w.t, "orgAdmin").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    await enable(w, w.manager, w.otherMachine); // another machine's
    const kept = await as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, {
      machineId: w.machine,
      name: "Gardee",
    });
    const gone = await as(w.t, "orgAdmin").mutation(api.consoleAccess.createDevice, {
      machineId: w.machine,
      name: "Revoquee",
    });
    await as(w.t, "orgAdmin").mutation(api.consoleAccess.revokeDevice, { deviceId: gone.deviceId });

    const before = Date.now();
    const answer = await fetchList(w, key);
    expect(answer.status).toBe("signed");
    expect(answer.server_contract_version).toBe(CONTRACT_VERSION);
    const payload = answer.payload ?? "";
    const verified = await crypto.subtle.verify(
      { name: "Ed25519" },
      keys.publicKey,
      unbase64(answer.signature ?? ""),
      new TextEncoder().encode(payload),
    );
    expect(verified).toBe(true);
    const list = JSON.parse(payload) as Payload;
    expect(list.kind).toBe(machineApi.operator_list.kind);
    expect(list.machineId).toBe(w.machine);
    expect(list.issuedAt).toBeGreaterThanOrEqual(before);
    expect(list.operators.map((o) => o.userId)).toEqual([w.orgAdmin, w.manager].sort());
    expect(list.operators.map((o) => o.name).sort()).toEqual(["manager Synthetic", "orgAdmin Synthetic"]);
    expect(list.operators[0].code).toMatchObject({ algorithm: "scrypt", n: OPERATOR_CODE.n });
    expect(list.devices).toEqual([
      { deviceId: kept.deviceId, name: "Gardee", tokenSha256: await tokenSha256(kept.token) },
    ]);
    expect(payload).not.toContain(kept.token);
    // The bytes signed are the payload as built, never serialised again.
    expect(
      operatorListPayload({
        machineId: list.machineId,
        issuedAt: list.issuedAt,
        operators: list.operators as never,
        devices: list.devices,
      }),
    ).toBe(payload);
  });

  it("drops a disabled operator, and one who is no longer a member who may be one", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, (await keyPair()).privateBase64);
    await enable(w, w.manager);
    await as(w.t, "manager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    await enable(w, w.otherManager);
    await as(w.t, "otherManager").action(api.consoleCodes.setMyOperatorCode, { code: aCode() });
    const listed = async () =>
      (JSON.parse((await fetchList(w, key)).payload ?? "") as Payload).operators.map((o) => o.userId);
    expect(await listed()).toEqual([w.manager, w.otherManager].sort());
    await as(w.t, "orgAdmin").mutation(api.consoleAccess.disableOperator, {
      machineId: w.machine,
      userId: w.manager,
    });
    expect(await listed()).toEqual([w.otherManager]);
    await w.t.run(async (ctx) => {
      const membership = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.otherManager).eq("organizationId", w.orgA),
        )
        .unique();
      if (membership !== null) await ctx.db.patch(membership._id, { active: false });
    });
    expect(await listed()).toEqual([]);
    const listedToAdmin = await as(w.t, "orgAdmin").query(api.consoleAccess.listConsoleAccess, {
      machineId: w.machine,
    });
    expect(listedToAdmin?.operators.find((o) => o.userId === w.otherManager)?.eligible).toBe(false);
  });

  it("answers not_configured on a key that is not an Ed25519 private key", async () => {
    const w = await seedWorld(modules);
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, btoa("not an Ed25519 private key"));
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    expect((await fetchList(w, await machineKey(w))).status).toBe("not_configured");
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });
});

describe("ANH-152 EX-5 the operator of a session is an account", () => {
  async function local(w: World, key: string, ref: string, operatorId?: string) {
    const response = await w.t.fetch("/api/machine/training/local", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${key}`,
        "Content-Type": "application/json",
        [CONTRACT_HEADER]: CONTRACT_VERSION,
      },
      body: JSON.stringify({
        localRef: ref,
        kind: "manual",
        startedAt: NOW,
        operatorName: operatorId ?? "Nom saisi",
        ...(operatorId === undefined ? {} : { operatorId }),
      }),
    });
    expect(response.status).toBe(200);
    return ((await response.json()) as { sessionId: Id<"sessions"> }).sessionId;
  }

  it("keeps an operator of the machine by account and names them; ignores any other", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    await enable(w, w.manager);
    const named = await local(w, key, "ref-1", w.manager);
    const stranger = await local(w, key, "ref-2", w.otherManager);
    const garbage = await local(w, key, "ref-3", "not an id");
    const older = await local(w, key, "ref-4");
    const read = (sessionId: Id<"sessions">) =>
      as(w.t, "orgAdmin").query(api.training.getTrainingSession, { sessionId });
    const stored = await w.t.run((ctx) => ctx.db.get(named));
    expect(stored?.operatorId).toBe(w.manager);
    expect(stored?.operatorName).toBeUndefined();
    expect((await read(named))?.operatorName).toBe("manager Synthetic");
    expect((await w.t.run((ctx) => ctx.db.get(stranger)))?.operatorId).toBeUndefined();
    expect((await read(stranger))?.operatorName).toBe(w.otherManager);
    expect((await read(garbage))?.operatorName).toBe("not an id");
    expect((await read(older))?.operatorName).toBe("Nom saisi");
  });

  it("stores an event a tablet made with nobody signed in", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const sessionId = await local(w, key, "ref-1");
    const send = (actor: string) =>
      w.t.fetch("/api/machine/training/events", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
          [CONTRACT_HEADER]: CONTRACT_VERSION,
        },
        body: JSON.stringify({
          sessionId,
          events: [{ seq: 0, t: NOW, kind: "estop", detail: "e-stop", actor }],
        }),
      });
    expect((await send("machine:local")).status).toBe(400);
    expect(await (await send("device:local")).json()).toEqual({
      stored: 1,
      duplicates: 0,
      rejected: 0,
    });
    const events = await w.t.run((ctx) => ctx.db.query("training_events").collect());
    expect(events.map((e) => e.actor)).toEqual(["device:local"]);
  });
});
