/// <reference types="vite/client" />
import { createHmac } from "node:crypto";
import { describe, expect, it } from "vitest";
import { api, internal } from "./_generated/api";
import { hashApiKey, verifyApiKey } from "./lib/crypto";
import { machineFixture } from "./machineAuth.fixtures";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
]);
const syntheticKey = `anh1.${"12".repeat(16)}.${"34".repeat(32)}`;
const knownDigest = `hmac-sha256:1:${"56".repeat(16)}:a226c69bd262b5c875cf539d4d87092a2544085f23502522f4d6739dd8a4e065`;

describe("ANH-121 salted credential storage and migration", () => {
  it("verifies the independently calculated HMAC-SHA-256 known answer", async () => {
    // Given a fixed synthetic credential and an independently calculated Node HMAC vector.
    // When / Then native verification accepts only the matching credential.
    expect(await verifyApiKey(syntheticKey, knownDigest)).toBe(true);
    expect(
      await verifyApiKey(syntheticKey.replace(/.$/, "5"), knownDigest),
    ).toBe(false);
  });

  it("salts each hash independently and matches the Node SHA-256 HMAC oracle", async () => {
    // Given identical credential input on two independent hashing calls.
    const first = await hashApiKey(syntheticKey);
    const second = await hashApiKey(syntheticKey);
    // When interpreting each stored verifier with an independent crypto implementation.
    const salts = [];
    for (const stored of [first, second]) {
      const [, , salt, digest] = stored.split(":");
      if (!salt || !digest) throw new TypeError("Missing salted digest");
      salts.push(salt);
      const oracle = createHmac("sha256", Buffer.from(salt, "hex"))
        .update(syntheticKey)
        .digest("hex");
      // Then the value is a salted HMAC-SHA-256 digest, not an encoding or another algorithm.
      expect(digest === oracle).toBe(true);
      expect(await verifyApiKey(syntheticKey, stored)).toBe(true);
    }
    expect(salts[0] === salts[1]).toBe(false);
    expect(first === second).toBe(false);
  });

  it.each([
    "",
    "legacy",
    "hmac-sha256:2:unknown",
    "hmac-sha256:1:56:34",
    knownDigest + "extra",
  ])(
    "rejects malformed or unknown stored credential versions (%#)",
    async (stored) => {
      // Given an unsupported persisted verifier.
      // When / Then it fails closed without a legacy fallback or parser exception.
      expect(await verifyApiKey(syntheticKey, stored)).toBe(false);
    },
  );

  it("replaces a synthetic reversible legacy record without accepting its old key", async () => {
    // Given a pre-migration record with no selector and a reversible credential.
    const f = await machineFixture(modules);
    const legacyKey = "synthetic-legacy-credential";
    const legacyStorage = btoa(legacyKey).split("").reverse().join("");
    await f.t.run((ctx) =>
      ctx.db.patch(f.machineId, {
        apiKey: legacyStorage,
        apiKeySelector: undefined,
      }),
    );
    const before = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: { Authorization: `Bearer ${legacyKey}` },
    });
    expect(before.status).toBe(401);
    for (const query of [
      internal.machines.getMachineByApiKey,
      internal.machines.validateMachineApiKey,
    ]) {
      expect(await f.t.query(query, { apiKey: legacyKey })).toBeNull();
    }
    // When an authorized operator regenerates its credential via the existing mutation.
    const replacement = await f.admin.mutation(api.machines.regenerateApiKey, {
      machineId: f.machineId,
    });
    // Then legacy storage is overwritten and only the new credential works.
    const stored = await f.t.run((ctx) => ctx.db.get(f.machineId));
    expect(stored?.apiKey === legacyStorage).toBe(false);
    expect(stored?.apiKey.startsWith("hmac-sha256:1:")).toBe(true);
    expect(stored?.apiKeySelector?.length).toBe(32);
    const oldResponse = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: { Authorization: `Bearer ${legacyKey}` },
    });
    const newResponse = await f.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: { Authorization: `Bearer ${replacement.apiKey}` },
    });
    expect(oldResponse.status).toBe(401);
    expect(newResponse.status).toBe(200);
  });

  it.each([
    { state: "disabled", patch: { authenticationEnabled: false } },
    { state: "deleted", patch: { isDeleted: true } },
  ])(
    "keeps a $state legacy machine revoked when regenerating",
    async ({ patch }) => {
      // Given a revoked legacy machine requiring credential cleanup.
      const f = await machineFixture(modules);
      await f.t.run((ctx) =>
        ctx.db.patch(f.machineId, {
          ...patch,
          apiKey: "synthetic-legacy-encoding",
          apiKeySelector: undefined,
        }),
      );
      // When / Then replacement does not reactivate the machine.
      const replacement = await f.admin.mutation(
        api.machines.regenerateApiKey,
        { machineId: f.machineId },
      );
      const response = await f.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers: { Authorization: `Bearer ${replacement.apiKey}` },
      });
      expect(response.status).toBe(401);
    },
  );

  it("requires an authorized operator to create or replace credentials", async () => {
    // Given an unauthenticated client that knows a machine ID.
    const f = await machineFixture(modules);
    // When / Then neither one-time secret issuance endpoint is available.
    await expect(
      f.t.mutation(api.machines.createMachine, { name: "Untrusted" }),
    ).rejects.toThrow();
    await expect(
      f.t.mutation(api.machines.regenerateApiKey, { machineId: f.machineId }),
    ).rejects.toThrow();
  });
});
