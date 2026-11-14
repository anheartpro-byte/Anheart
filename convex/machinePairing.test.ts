/**
 * ANH-115 pairing codes (contract 1.17): a console with no key trades a
 * one-time code, with its Pi's serial number, for one
 * (`POST /api/machine/pair`). The code is shown once, kept as a salted scrypt
 * hash only, bound to one serial number, open 30 minutes, one claim, burned by
 * five wrong codes; the claim gives the key once and revokes the others.
 */
import * as crypto from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import schema from "./schema";
import { CONTRACT_VERSION } from "./lib/contract";
import { PAIRING_MAX_FAILURES, PAIRING_TTL_MS } from "./lib/machinePairing";
import { machineHeaders } from "./machineAuth.fixtures";
import {
  issuedCode,
  pair,
  PI_SERIAL,
  withPiSerial,
} from "./machinePairing.fixtures";
import { as, modules, NOW, seedWorld, type World } from "./test.setup";

// Every scrypt of the backend, counted: an unknown serial number costs one too.
vi.mock("node:crypto", async (original) => {
  const actual = await original<typeof crypto>();
  return { ...actual, scrypt: vi.fn(actual.scrypt) };
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

/** A wrong code for `code`: the same form, another first character. */
function wrongFor(code: string): string {
  return `${code.startsWith("0") ? "1" : "0"}${code.slice(1)}`;
}

/** The machine's pairing codes, oldest first. */
async function codesOf(w: World, machineId: Id<"machines"> = w.machine) {
  const rows = await w.t.run((ctx) =>
    ctx.db
      .query("machine_pairing_codes")
      .withIndex("by_machine_and_closed", (q) => q.eq("machineId", machineId))
      .collect(),
  );
  return rows.sort((a, b) => a._creationTime - b._creationTime);
}

/** A world whose machine's registry names the Pi, with one key of its own. */
async function pairable() {
  const w = await seedWorld(modules);
  await withPiSerial(w);
  const old = await as(w.t, "admin").mutation(
    api.machineKeys.createMachineKey,
    {
      machineId: w.machine,
    },
  );
  return { w, old };
}

async function refusalOf(response: Response) {
  return { status: response.status, body: (await response.json()) as unknown };
}

const REFUSAL = {
  status: 401,
  body: {
    error: "pairing_refused",
    message: "No open pairing code of this Pi matches",
  },
};

describe("ANH-115 EX-1 a one-time pairing code, issued from the site", () => {
  it("serves a machine created without a key: its first key is the one its code gives", async () => {
    const w = await seedWorld(modules);
    const created = await as(w.t, "admin").mutation(
      api.machines.createMachine,
      {
        name: "Synthetic machine to pair",
        organizationId: w.orgA,
        keyless: true,
      },
    );
    expect(created.apiKey).toBeNull();
    const keys = () =>
      w.t.run((ctx) =>
        ctx.db
          .query("machine_api_keys")
          .withIndex("by_machine", (q) => q.eq("machineId", created.machineId))
          .collect(),
      );
    expect(await keys()).toEqual([]);
    const audited = await w.t.run((ctx) =>
      ctx.db
        .query("audit_log")
        .filter((q) => q.eq(q.field("targetId"), created.machineId))
        .collect(),
    );
    expect(audited.map((row) => row.action)).toEqual(["machine.created"]);
    await withPiSerial(w, created.machineId);
    const { code } = await issuedCode(w, "admin", created.machineId);
    const response = await pair(w.t, { code, serialPi: PI_SERIAL });
    expect(response.status).toBe(200);
    expect(await keys()).toHaveLength(1);
  });

  it("is eight Crockford characters, open 30 minutes, kept as a salted scrypt hash only", async () => {
    const { w } = await pairable();
    const { code, expiresAt } = await issuedCode(w, "orgAdmin");
    expect(code).toMatch(/^[0-9A-HJKMNP-TV-Z]{4}-[0-9A-HJKMNP-TV-Z]{4}$/);
    expect(expiresAt).toBe(NOW + PAIRING_TTL_MS);
    const [row] = await codesOf(w);
    expect(row).toMatchObject({
      serialPi: PI_SERIAL,
      failures: 0,
      createdBy: w.orgAdmin,
      expiresAt,
    });
    expect(Buffer.from(row.salt, "base64")).toHaveLength(16);
    expect(Buffer.from(row.hash, "base64")).toHaveLength(32);
    expect(JSON.stringify(row)).not.toContain(code.replace("-", ""));
  });

  it("serves the Pi the registry names, in the registry's words but compared as the Pi reads it", async () => {
    const w = await seedWorld(modules);
    await withPiSerial(w, w.machine, `  ${PI_SERIAL.toUpperCase()} `);
    const { code } = await issuedCode(w);
    expect((await codesOf(w))[0].serialPi).toBe(PI_SERIAL);
    expect((await pair(w.t, { code, serialPi: PI_SERIAL })).status).toBe(200);
  });

  it("is refused for a machine whose registry names no Pi, with nothing written", async () => {
    const w = await seedWorld(modules);
    await expect(issuedCode(w)).rejects.toThrow(/pairing_serial_missing/);
    await withPiSerial(w, w.machine, "not a serial");
    await expect(issuedCode(w)).rejects.toThrow(/pairing_serial_missing/);
    expect(await codesOf(w)).toEqual([]);
  });

  it("is refused while another machine has a code open for the same Pi", async () => {
    const w = await seedWorld(modules);
    await withPiSerial(w);
    await withPiSerial(w, w.otherMachine);
    await issuedCode(w);
    await expect(issuedCode(w, "admin", w.otherMachine)).rejects.toThrow(
      /pairing_serial_in_use/,
    );
    await as(w.t, "admin").mutation(api.machinePairing.revokePairingCode, {
      machineId: w.machine,
    });
    await issuedCode(w, "admin", w.otherMachine);
  });

  it("replaces the machine's open code: the earlier one is refused", async () => {
    const { w } = await pairable();
    const first = await issuedCode(w);
    const second = await issuedCode(w);
    expect(
      await refusalOf(
        await pair(w.t, { code: first.code, serialPi: PI_SERIAL }),
      ),
    ).toEqual(REFUSAL);
    expect(
      (await pair(w.t, { code: second.code, serialPi: PI_SERIAL })).status,
    ).toBe(200);
    expect((await codesOf(w)).map((row) => row.closedAs)).toEqual([
      "replaced",
      "claimed",
    ]);
  });

  it("says on the registry whether a code is open and until when, never the code; a revoked code serves no more", async () => {
    const { w } = await pairable();
    const { code, expiresAt } = await issuedCode(w);
    const state = await as(w.t, "orgAdmin").query(
      api.machinePairing.pairingState,
      {
        machineId: w.machine,
      },
    );
    expect(state).toEqual({
      createdAt: NOW,
      expiresAt,
      createdByName: expect.any(String),
      serverNow: NOW,
    });
    expect(JSON.stringify(state)).not.toContain(code.replace("-", ""));
    await as(w.t, "orgAdmin").mutation(api.machinePairing.revokePairingCode, {
      machineId: w.machine,
    });
    expect(
      await as(w.t, "orgAdmin").query(api.machinePairing.pairingState, {
        machineId: w.machine,
      }),
    ).toBeNull();
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
  });

  it("is said no more once it expires, and serves no more", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    vi.setSystemTime(NOW + PAIRING_TTL_MS);
    expect(
      await as(w.t, "admin").query(api.machinePairing.pairingState, {
        machineId: w.machine,
      }),
    ).toBeNull();
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
  });

  it("closes an expired code without a revocation in the audit when another is issued", async () => {
    const { w } = await pairable();
    await issuedCode(w);
    vi.setSystemTime(NOW + PAIRING_TTL_MS);
    await issuedCode(w);
    expect((await codesOf(w)).map((row) => row.closedAs)).toEqual([
      "replaced",
      undefined,
    ]);
    const revoked = await w.t.run((ctx) =>
      ctx.db
        .query("audit_log")
        .filter((q) => q.eq(q.field("action"), "machine_pairing.revoked"))
        .collect(),
    );
    expect(revoked).toEqual([]);
  });

  it("says no author for a code whose author is gone", async () => {
    const { w } = await pairable();
    await issuedCode(w, "orgAdmin");
    await w.t.run((ctx) => ctx.db.delete(w.orgAdmin));
    expect(
      await as(w.t, "admin").query(api.machinePairing.pairingState, {
        machineId: w.machine,
      }),
    ).toMatchObject({ createdByName: null });
  });

  it("is refused for a machine of no organisation", async () => {
    const { w } = await pairable();
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { organizationId: undefined }),
    );
    await expect(issuedCode(w)).rejects.toThrow(/machine_not_found/);
  });
});

describe("ANH-115 EX-2 POST /api/machine/pair", () => {
  it("gives the machine's new key once, revokes every other key at once, and is not cached", async () => {
    const { w, old } = await pairable();
    const { code } = await issuedCode(w);
    const response = await pair(w.t, { code, serialPi: PI_SERIAL });
    expect(response.status).toBe(200);
    expect(response.headers.get("Cache-Control")).toBe("no-store");
    const body = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(body).sort()).toEqual(
      [
        "apiKey",
        "convexUrl",
        "keyPrefix",
        "machineId",
        "server_contract_version",
      ].sort(),
    );
    expect(body).toMatchObject({
      machineId: w.machine,
      server_contract_version: CONTRACT_VERSION,
    });
    expect(body.convexUrl).toMatch(/^https?:\/\//);
    const apiKey = body.apiKey as string;
    expect(apiKey.startsWith(body.keyPrefix as string)).toBe(true);
    const roster = (key: string) =>
      w.t.fetch("/api/machine/roster", {
        method: "GET",
        headers: machineHeaders(key),
      });
    expect((await roster(apiKey)).status).toBe(200);
    expect((await roster(old.apiKey)).status).toBe(401);
    const [row] = await codesOf(w);
    expect(row).toMatchObject({ closedAs: "claimed", closedAt: NOW });
    expect(row.keyId).toBeDefined();
  });

  it("serves once: the same code again is refused", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    expect((await pair(w.t, { code, serialPi: PI_SERIAL })).status).toBe(200);
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
  });

  it("reads the code as it is typed: lower case, spaces, O for 0, I and L for 1", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    const typed = ` ${code.toLowerCase().replace(/0/g, "o").replace(/1/g, "l").replace("-", " ")} `;
    expect(
      (await pair(w.t, { code: typed, serialPi: PI_SERIAL.toUpperCase() }))
        .status,
    ).toBe(200);
  });

  it("is bound to the Pi: the right code with another serial number is refused and counts nothing", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: "another-pi" })),
    ).toEqual(REFUSAL);
    expect((await codesOf(w))[0].failures).toBe(0);
    expect((await pair(w.t, { code, serialPi: PI_SERIAL })).status).toBe(200);
  });

  it("takes four wrong codes and still serves the right one", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    for (let attempt = 1; attempt < PAIRING_MAX_FAILURES; attempt += 1)
      expect(
        await refusalOf(
          await pair(w.t, { code: wrongFor(code), serialPi: PI_SERIAL }),
        ),
      ).toEqual(REFUSAL);
    expect((await codesOf(w))[0].failures).toBe(PAIRING_MAX_FAILURES - 1);
    expect((await pair(w.t, { code, serialPi: PI_SERIAL })).status).toBe(200);
  });

  it("is burned by its fifth wrong code: the right one is refused after", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    for (let attempt = 0; attempt < PAIRING_MAX_FAILURES; attempt += 1)
      await pair(w.t, { code: wrongFor(code), serialPi: PI_SERIAL });
    expect((await codesOf(w))[0]).toMatchObject({
      closedAs: "burned",
      failures: 5,
    });
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
  });

  it("refuses in the same words, after the same scrypt work, whatever the reason", async () => {
    const { w } = await pairable();
    const scrypt = vi.mocked(crypto.scrypt);
    const { code } = await issuedCode(w);
    const answers: unknown[] = [];
    const works: number[] = [];
    const sent = async (body: unknown) => {
      scrypt.mockClear();
      answers.push(await refusalOf(await pair(w.t, body)));
      works.push(scrypt.mock.calls.length);
    };
    await sent({ code, serialPi: "a-pi-with-no-code" });
    await sent({ code: wrongFor(code), serialPi: PI_SERIAL });
    expect((await pair(w.t, { code, serialPi: PI_SERIAL })).status).toBe(200);
    await sent({ code, serialPi: PI_SERIAL });
    const expired = await issuedCode(w);
    vi.setSystemTime(NOW + PAIRING_TTL_MS);
    await sent({ code: expired.code, serialPi: PI_SERIAL });
    expect(answers).toEqual(answers.map(() => REFUSAL));
    expect(works).toEqual([1, 1, 1, 1]);
  });

  it("takes the global bucket before anything else of the request", async () => {
    const w = await seedWorld(modules);
    const noContract = () =>
      pair(w.t, { code: "ABCD-EFGH", serialPi: PI_SERIAL }, {});
    for (let request = 0; request < 20; request += 1)
      expect((await noContract()).status).toBe(426);
    const limited = await noContract();
    expect(limited.status).toBe(429);
    expect(limited.headers.get("Retry-After")).not.toBeNull();
    expect(((await limited.json()) as { error: string }).error).toBe(
      "rate_limited",
    );
    vi.setSystemTime(NOW + 60_000);
    expect((await noContract()).status).toBe(426);
  });

  it("answers the contract header, the size and the form before any code is looked at", async () => {
    const w = await seedWorld(modules);
    const scrypt = vi.mocked(crypto.scrypt);
    scrypt.mockClear();
    expect(
      (await pair(w.t, { code: "ABCD-EFGH", serialPi: PI_SERIAL }, {})).status,
    ).toBe(426);
    expect((await pair(w.t, "x".repeat(1025))).status).toBe(413);
    for (const body of [
      "not json",
      [],
      {},
      { code: "ABCD-EFG", serialPi: PI_SERIAL },
      { code: "ABCD-EFGU", serialPi: PI_SERIAL },
      { code: "ABCD-EFGH", serialPi: "" },
      { code: "ABCD-EFGH", serialPi: "x".repeat(65) },
      { code: "ABCD-EFGH", serialPi: 42 },
    ]) {
      const response = await pair(w.t, body);
      expect(response.status, JSON.stringify(body)).toBe(400);
      expect(((await response.json()) as { error: string }).error).toBe(
        "invalid_request",
      );
    }
    expect(scrypt).not.toHaveBeenCalled();
  });

  it("never keeps the plain key or the code: no log line, no audit row, no stored row", async () => {
    const { w } = await pairable();
    const logged = (["log", "info", "warn", "error", "debug"] as const).map(
      (level) => vi.spyOn(console, level),
    );
    const { code } = await issuedCode(w);
    await pair(w.t, { code: wrongFor(code), serialPi: PI_SERIAL });
    const body = (await (
      await pair(w.t, { code, serialPi: PI_SERIAL })
    ).json()) as {
      apiKey: string;
    };
    const secret = body.apiKey.split(".")[2];
    expect(secret.length).toBeGreaterThan(16);
    const stored = await w.t.run(async (ctx) => {
      const tables: Record<string, unknown[]> = {};
      for (const table of Object.keys(
        schema.tables,
      ) as (keyof typeof schema.tables)[])
        tables[table] = await ctx.db.query(table).collect();
      return JSON.stringify(tables);
    });
    expect(stored).toContain("machine_pairing.claimed");
    const printed = JSON.stringify(logged.map((spy) => spy.mock.calls));
    for (const kept of [stored, printed])
      for (const plain of [body.apiKey, secret, code, code.replace("-", "")])
        expect(kept).not.toContain(plain);
  });
});

describe("ANH-115 the claim reads the code again in its transaction", () => {
  async function openId(w: World) {
    const [row] = await codesOf(w);
    return row._id;
  }

  it("applies verdicts one after the other: five wrong burn it, a right one after is refused", async () => {
    const { w } = await pairable();
    await issuedCode(w);
    const pairingId = await openId(w);
    for (let attempt = 0; attempt < PAIRING_MAX_FAILURES; attempt += 1)
      expect(
        await w.t.mutation(internal.machinePairing.claimPairing, {
          pairingId,
          matched: false,
        }),
      ).toEqual({ status: "refused" });
    expect(
      await w.t.mutation(internal.machinePairing.claimPairing, {
        pairingId,
        matched: true,
      }),
    ).toEqual({ status: "refused" });
    expect((await codesOf(w))[0]).toMatchObject({
      closedAs: "burned",
      failures: 5,
    });
  });

  it("claims once: a second right verdict is refused, and so is one after a revocation", async () => {
    const { w } = await pairable();
    await issuedCode(w);
    const pairingId = await openId(w);
    const claim = () =>
      w.t.mutation(internal.machinePairing.claimPairing, {
        pairingId,
        matched: true,
      });
    expect((await claim()).status).toBe("claimed");
    expect(await claim()).toEqual({ status: "refused" });
    await issuedCode(w);
    const [, second] = await codesOf(w);
    await as(w.t, "admin").mutation(api.machinePairing.revokePairingCode, {
      machineId: w.machine,
    });
    expect(
      await w.t.mutation(internal.machinePairing.claimPairing, {
        pairingId: second._id,
        matched: true,
      }),
    ).toEqual({ status: "refused" });
  });

  it.each([
    ["deleted", { isDeleted: true }],
    ["disabled", { authenticationEnabled: false }],
  ] as const)("pairs no %s machine", async (_state, patch) => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    await w.t.run((ctx) => ctx.db.patch(w.machine, patch));
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
  });

  it("pairs no machine whose registry names another Pi since the code was issued", async () => {
    const { w } = await pairable();
    const { code } = await issuedCode(w);
    await withPiSerial(w, w.machine, "a-new-pi");
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: PI_SERIAL })),
    ).toEqual(REFUSAL);
    expect(
      await refusalOf(await pair(w.t, { code, serialPi: "a-new-pi" })),
    ).toEqual(REFUSAL);
  });
});
