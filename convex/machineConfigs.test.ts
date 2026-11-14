/// <reference types="vite/client" />
/**
 * ANH-141: the machine configuration. The validation the console applies
 * (`contracts/machine-config-cases.json`, word for word), the canonical text
 * and its hash, the double approval of a safety key, the organisation admin's
 * two keys, the two locks held to the registry's level at proposal and at
 * approval, the signed text a machine fetches, what it says it applied or
 * refused, and the status the machine page shows. Who may call each public
 * function, cell by cell, is `authorization.matrix.ts`.
 *
 * Every key here is made at run time: no secret is written anywhere.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { CATALOG_SIGNING_KEY_ENV } from "./lib/programmes";
import {
  canonicalConfig,
  changedKeys,
  configHash,
  configurationStatus,
  CONFIG_KEY_NAMES,
  machineConfigProblems,
  pythonFloat,
  SAFETY_KEYS,
  type ConfigValues,
} from "./lib/machineConfig";
import type { MachineValidationLevel } from "./lib/releaseValidation";
import { registryUpload } from "./machineRegistry.fixtures";
import { addSecondAdmin, asSecondAdmin } from "./programmes.fixtures";
import { as, modules, seedWorld, type World } from "./test.setup";
import cases from "../contracts/machine-config-cases.json";
import randomCases from "../contracts/machine-config-random-cases.json";
import machineApi from "../contracts/machine-api.json";

// The machine routes are rate limited per machine: each request below comes
// later than the last by more than the longest wait between two.
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(Date.UTC(2026, 10, 14, 12));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

type Case = {
  name: string;
  changes: Record<string, unknown>;
  remove?: string[];
  level?: MachineValidationLevel;
  expected: { verdict: "accepted" | "refused"; problems?: { key: string; detail: string }[] };
};

const BASE = cases.base as ConfigValues;

describe("ANH-141 EX-2 the validation is the console's", () => {
  it.each((cases.cases as Case[]).map((c) => [c.name, c] as const))("%s", (_name, c) => {
    const document = { ...BASE, ...c.changes } as ConfigValues;
    for (const key of c.remove ?? []) delete document[key];
    const problems = machineConfigProblems(document, c.level ?? "none");
    expect(problems).toEqual(c.expected.problems ?? []);
    expect(c.expected.verdict === "accepted").toBe(problems.length === 0);
  });

  it("gives the console's verdict on its seeded random proposals", () => {
    // raspberry-pi/tests/test_machine_config_random.py draws them and pins the file.
    const drawn = randomCases as unknown as Array<{
      name: string;
      changes: Record<string, unknown>;
      remove: string[];
      level: MachineValidationLevel;
      problems: { key: string; detail: string }[];
    }>;
    expect(drawn.length).toBe(300);
    const differ = drawn.filter((c) => {
      const document = { ...BASE, ...c.changes } as ConfigValues;
      for (const key of c.remove) delete document[key];
      return JSON.stringify(machineConfigProblems(document, c.level)) !== JSON.stringify(c.problems);
    });
    expect(differ.map((c) => c.name)).toEqual([]);
  });

  it.each([
    [0, "0.0"],
    [-1, "-1.0"],
    [1.5, "1.5"],
    [300, "300.0"],
    [0.0001, "0.0001"],
    [0.00001, "1e-05"],
    [-0.000012, "-1.2e-05"],
    [1234567890123456, "1234567890123456.0"],
    [1e16, "1e+16"],
    [1.5e300, "1.5e+300"],
    [123456.789, "123456.789"],
  ])("writes %d as Python writes it: %s", (value, text) => {
    expect(pythonFloat(value)).toBe(text);
  });

  it("refuses a key the console does not read, and one missing", () => {
    const document: ConfigValues = { ...BASE, toString: "x" };
    delete document.MOTOR_PORT;
    expect(machineConfigProblems(document, "none")).toEqual([
      { key: "MOTOR_PORT", detail: "cle absente" },
      { key: "toString", detail: "cle inconnue : la console ne la revalide pas" },
    ]);
  });
});

describe("ANH-141 EX-6 the canonical text and its hash", () => {
  it("sorts the keys, writes no space, and hashes the UTF-8 bytes", async () => {
    const text = canonicalConfig({ b: 1, a: [" x"], c: null, d: "é" });
    expect(text).toBe('{"a":[" x"],"b":1,"c":null,"d":"é"}');
    const expected = Array.from(
      new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text))),
      (byte) => byte.toString(16).padStart(2, "0"),
    ).join("");
    expect(await configHash(text)).toBe(expected);
    expect(await configHash(text)).toMatch(new RegExp(machineApi.machine_config.hash_pattern));
  });

  it("names every key changed, all of them without a base", () => {
    expect(changedKeys(null, BASE)).toEqual(CONFIG_KEY_NAMES);
    expect(changedKeys(BASE, { ...BASE, location: "Ailleurs", MOTOR_MAX_RPM: 400 })).toEqual([
      "location",
      "MOTOR_MAX_RPM",
    ]);
    expect(SAFETY_KEYS).toEqual([
      "MOTOR_BACKEND",
      "MOTOR_MAX_RPM",
      "ARM_RADIUS_M",
      "LEG_TIP_RADIUS_M",
      "PROGRAMS_ENABLED",
      "OCCUPANCY_OCCUPIED_ENABLED",
    ]);
  });

  it("says pending, applied or mismatch from the revision in force and the reported hash", () => {
    expect(configurationStatus(null, "a")).toBe("unavailable");
    expect(configurationStatus({ status: "approved", hash: "a" }, "a")).toBe("pending");
    expect(configurationStatus({ status: "applied", hash: "a" }, "a")).toBe("applied");
    expect(configurationStatus({ status: "applied", hash: "a" }, null)).toBe("mismatch");
  });
});

// ---------------------------------------------------------------------------
// The flow, on a seeded world
// ---------------------------------------------------------------------------

const base64 = (bytes: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const unbase64 = (text: string) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));

async function keyPair() {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  return {
    privateBase64: base64(await crypto.subtle.exportKey("pkcs8", pair.privateKey)),
    publicKey: pair.publicKey,
  };
}

async function machineKey(w: World, machineId = w.machine) {
  return (await as(w.t, "admin").mutation(api.machineKeys.createMachineKey, { machineId })).apiKey;
}

function machineFetch(w: World, key: string, path: string, body?: unknown) {
  vi.setSystemTime(Date.now() + 10_001);
  return w.t.fetch(path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
      "Content-Type": "application/json",
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

type Payload = {
  kind: string;
  machineId: string;
  issuedAt: number;
  validationLevel: string;
  revision: number | null;
  hash: string | null;
  values: string | null;
};

async function fetchConfig(w: World, key: string, verifier: CryptoKey): Promise<Payload> {
  const response = await machineFetch(w, key, "/api/machine/config");
  expect(response.status).toBe(200);
  const answer = (await response.json()) as { status: string; payload: string; signature: string };
  expect(answer.status).toBe("signed");
  const verified = await crypto.subtle.verify(
    { name: "Ed25519" },
    verifier,
    unbase64(answer.signature),
    new TextEncoder().encode(answer.payload),
  );
  expect(verified).toBe(true);
  return JSON.parse(answer.payload) as Payload;
}

/** The machine's registry validated to `level`, by consecutive reviews in the past. */
async function setLevel(w: World, level: MachineValidationLevel, machineId = w.machine) {
  const order = ["bench", "auto", "occupied"] as const;
  const reviews: Doc<"machine_registry">["validation"]["reviews"] = [];
  for (const [index, reviewed] of order.slice(0, order.indexOf(level as never) + 1).entries())
    reviews.push({
      level: reviewed,
      by: w.admin,
      at: Date.now() - 10_000 + index,
      reportStorageId: await registryUpload(w),
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: "0.1.0",
    });
  await w.t.run(async (ctx) => {
    const existing = await ctx.db
      .query("machine_registry")
      .withIndex("by_machine", (q) => q.eq("machineId", machineId))
      .unique();
    const validation = { level, reviews, downgrades: [] };
    if (existing === null)
      await ctx.db.insert("machine_registry", {
        machineId,
        organizationId: (await ctx.db.get(machineId))?.organizationId,
        contacts: [],
        driveParameterSheetHistory: [],
        validation,
        updatedAt: Date.now(),
        updatedBy: w.admin,
      });
    else await ctx.db.patch(existing._id, { validation });
  });
}

const propose = (w: World, values: ConfigValues, actor: "admin" | "orgAdmin" = "admin") =>
  as(w.t, actor).mutation(api.machineConfigs.proposeConfig, { machineId: w.machine, values });

const rowOf = (w: World, configId: Id<"machine_configs">) =>
  w.t.run((ctx) => ctx.db.get(configId));

const auditOf = (w: World, targetId: string) =>
  w.t.run(async (ctx) =>
    (await ctx.db.query("audit_log").collect())
      .filter((entry) => entry.targetId === targetId)
      .map((entry) => ({ action: entry.action, fields: entry.details?.fields, version: entry.details?.version })),
  );

/** A first revision, approved by a second Anheart admin: what the machine is sent. */
async function approvedFirst(w: World, values: ConfigValues = BASE) {
  await addSecondAdmin(w);
  const first = await propose(w, values);
  await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: first.configId });
  return first;
}

async function applyOnMachine(w: World, key: string, revision: number, hash: string) {
  const response = await machineFetch(w, key, "/api/machine/config/applied", { revision, hash });
  expect(response.status).toBe(200);
  return ((await response.json()) as { status: string }).status;
}

describe("ANH-141 EX-2 EX-3 propose and approve", () => {
  it("refuses every problem of a proposal at once, in the console's words", async () => {
    const w = await seedWorld(modules);
    const refused = propose(w, { ...BASE, MOTOR_MAX_RPM: 2000, ARM_RADIUS_M: 0 });
    await expect(refused).rejects.toMatchObject({
      data: {
        code: "invalid_machine_config",
        problems: [
          { key: "ARM_RADIUS_M", detail: "0.0 m hors de ]0, 5.0]" },
          { key: "MOTOR_MAX_RPM", detail: "2000 tr/min hors de 0..1380 (plaque signaletique)" },
        ],
      },
    });
  });

  it("needs two distinct Anheart admins for the first revision, then the machine follows", async () => {
    const w = await seedWorld(modules);
    const second = await addSecondAdmin(w);
    const first = await propose(w, { ...BASE, name: "Banc B", location: "Lyon" });
    expect(first.revision).toBe(1);
    const draft = await rowOf(w, first.configId);
    expect(draft?.status).toBe("draft");
    expect(draft?.changedKeys).toEqual(CONFIG_KEY_NAMES);
    // The author may not approve a safety change, whatever the button shows.
    await expect(
      as(w.t, "admin").mutation(api.machineConfigs.approveConfig, { configId: first.configId }),
    ).rejects.toThrow(/machine_config_self_approval/);
    const view = await as(w.t, "admin").query(api.machineConfigs.getMachineConfig, { machineId: w.machine });
    expect(view?.revisions[0].approveBlocked).toBe("machine_config_self_approval");
    expect(view?.revisions[0].safetyChange).toBe(true);
    const asSecond = await asSecondAdmin(w).query(api.machineConfigs.getMachineConfig, { machineId: w.machine });
    expect(asSecond?.revisions[0].approveBlocked).toBeNull();

    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: first.configId });
    const approved = await rowOf(w, first.configId);
    expect(approved?.status).toBe("approved");
    expect(approved?.approvedBy).toEqual([w.admin, second]);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect([machine?.name, machine?.location]).toEqual(["Banc B", "Lyon"]);
    expect(await auditOf(w, first.configId)).toEqual([
      { action: "machine_config.proposed", fields: CONFIG_KEY_NAMES, version: "1" },
      { action: "machine_config.approved", fields: CONFIG_KEY_NAMES, version: "1" },
    ]);
  });

  it("lets the author approve a change of exploitation keys alone", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    const row = await rowOf(w, first.configId);
    expect(await applyOnMachine(w, key, 1, row?.hash ?? "")).toBe("applied");
    const next = await propose(w, { ...BASE, location: "Atelier 2", UI_PORT: 8081 });
    expect((await rowOf(w, next.configId))?.changedKeys).toEqual(["location", "UI_PORT"]);
    await as(w.t, "admin").mutation(api.machineConfigs.approveConfig, { configId: next.configId });
    expect((await rowOf(w, next.configId))?.approvedBy).toEqual([w.admin]);
  });

  it("asks a second admin again for a safety key changed after an application", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    const next = await propose(w, { ...BASE, MOTOR_MAX_RPM: 400 });
    await expect(
      as(w.t, "admin").mutation(api.machineConfigs.approveConfig, { configId: next.configId }),
    ).rejects.toThrow(/machine_config_self_approval/);
    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: next.configId });
    expect((await rowOf(w, next.configId))?.status).toBe("approved");
  });

  it("lets an organisation admin change the name and the place only", async () => {
    const w = await seedWorld(modules);
    // Nothing in force yet: every key would be new.
    await expect(propose(w, BASE, "orgAdmin")).rejects.toThrow(/machine_config_keys_not_allowed/);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    await expect(propose(w, { ...BASE, MOTOR_MAX_RPM: 200 }, "orgAdmin")).rejects.toThrow(
      /machine_config_keys_not_allowed/,
    );
    const renamed = await propose(w, { ...BASE, name: "Banc du centre", location: null }, "orgAdmin");
    expect((await rowOf(w, renamed.configId))?.proposedByRole).toBe("org_admin");
    await as(w.t, "orgAdmin").mutation(api.machineConfigs.approveConfig, { configId: renamed.configId });
    expect((await rowOf(w, renamed.configId))?.approvedBy).toEqual([w.orgAdmin]);
    const machine = await w.t.run((ctx) => ctx.db.get(w.machine));
    expect([machine?.name, machine?.location]).toEqual(["Banc du centre", undefined]);
    // An Anheart admin's draft of a safety key: the organisation admin cannot approve it.
    const safety = await propose(w, { ...BASE, name: "Banc du centre", location: null, MOTOR_MAX_RPM: 250 });
    await expect(
      as(w.t, "orgAdmin").mutation(api.machineConfigs.approveConfig, { configId: safety.configId }),
    ).rejects.toThrow(/machine_config_keys_not_allowed/);
  });

  it("refuses an organisation admin's draft carrying a safety change not yet applied", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    const pending = await propose(w, { ...BASE, MOTOR_MAX_RPM: 400 });
    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: pending.configId });
    // Renamed on top of the safety change the machine has not applied yet.
    const renamed = await propose(w, { ...BASE, MOTOR_MAX_RPM: 400, name: "Banc C" }, "orgAdmin");
    await expect(
      asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: renamed.configId }),
    ).rejects.toThrow(/machine_config_safety_needs_admins/);
  });

  it("refuses a revision equal to the one in force", async () => {
    const w = await seedWorld(modules);
    await approvedFirst(w);
    await expect(propose(w, BASE)).rejects.toThrow(/machine_config_unchanged/);
  });

  it("replaces a draft waiting, and an approved revision the machine has not applied", async () => {
    const w = await seedWorld(modules);
    const first = await approvedFirst(w);
    const draft = await propose(w, { ...BASE, location: "Un" });
    const next = await propose(w, { ...BASE, location: "Deux" });
    expect(await rowOf(w, draft.configId)).toMatchObject({
      status: "rejected",
      rejectReason: "remplacée par la révision 3",
      rejectedBy: w.admin,
    });
    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: next.configId });
    expect(await rowOf(w, first.configId)).toMatchObject({
      status: "rejected",
      rejectReason: "remplacée par la révision 3 avant son application",
    });
    await expect(
      asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: next.configId }),
    ).rejects.toThrow(/machine_config_wrong_status/);
  });

  it("refuses a deleted machine and a revision out of reach", async () => {
    const w = await seedWorld(modules);
    const first = await propose(w, BASE);
    await expect(
      as(w.t, "orgBAdmin").mutation(api.machineConfigs.approveConfig, { configId: first.configId }),
    ).rejects.toThrow(/machine_config_not_found/);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: true }));
    await expect(propose(w, BASE)).rejects.toThrow(/machine_not_found/);
    const view = await as(w.t, "admin").query(api.machineConfigs.getMachineConfig, { machineId: w.machine });
    expect(view?.canPropose).toBe("none");
  });
});

describe("ANH-141 EX-5 the locks follow the registry's level", () => {
  it("refuses PROGRAMS_ENABLED on a machine not validated to auto, with the reason", async () => {
    const w = await seedWorld(modules);
    await setLevel(w, "bench");
    await expect(propose(w, { ...BASE, PROGRAMS_ENABLED: true })).rejects.toMatchObject({
      data: {
        code: "invalid_machine_config",
        problems: [{ key: "PROGRAMS_ENABLED", detail: "machine validée jusqu'au niveau bench" }],
      },
    });
  });

  it("judges the level again at approval", async () => {
    const w = await seedWorld(modules);
    await addSecondAdmin(w);
    await setLevel(w, "auto");
    const first = await propose(w, { ...BASE, PROGRAMS_ENABLED: true });
    await setLevel(w, "bench");
    await expect(
      asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: first.configId }),
    ).rejects.toMatchObject({ data: { code: "invalid_machine_config" } });
    await setLevel(w, "auto");
    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: first.configId });
    expect((await rowOf(w, first.configId))?.status).toBe("approved");
  });

  it("accepts a person on board only at the occupied level", async () => {
    const w = await seedWorld(modules);
    await setLevel(w, "auto");
    const locks = { ...BASE, PROGRAMS_ENABLED: true, OCCUPANCY_OCCUPIED_ENABLED: true };
    await expect(propose(w, locks)).rejects.toThrow(/invalid_machine_config/);
    await setLevel(w, "occupied");
    expect((await propose(w, locks)).revision).toBe(1);
  });
});

describe("ANH-141 EX-4 EX-6 the machine's side", () => {
  it("signs the revision in force with its canonical values and the machine's level", async () => {
    const w = await seedWorld(modules);
    const keys = await keyPair();
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, keys.privateBase64);
    const key = await machineKey(w);
    const before = Date.now();
    const empty = await fetchConfig(w, key, keys.publicKey);
    expect(empty).toMatchObject({
      kind: machineApi.machine_config.kind,
      machineId: w.machine,
      validationLevel: "none",
      revision: null,
      hash: null,
      values: null,
    });
    expect(empty.issuedAt).toBeGreaterThanOrEqual(before);
    await setLevel(w, "auto");
    const first = await approvedFirst(w, { ...BASE, PROGRAMS_ENABLED: true });
    // A draft is never sent.
    await propose(w, { ...BASE, PROGRAMS_ENABLED: true, location: "Brouillon" });
    const sent = await fetchConfig(w, key, keys.publicKey);
    expect(sent).toMatchObject({ revision: 1, validationLevel: "auto" });
    expect(sent.values).toBe(canonicalConfig({ ...BASE, PROGRAMS_ENABLED: true }));
    expect(sent.hash).toBe(await configHash(sent.values ?? ""));
    expect(sent.hash).toBe((await rowOf(w, first.configId))?.hash);
  });

  it("answers not_configured without a signing key", async () => {
    const w = await seedWorld(modules);
    const response = await machineFetch(w, await machineKey(w), "/api/machine/config");
    expect(await response.json()).toEqual({
      status: "not_configured",
      server_contract_version: CONTRACT_VERSION,
    });
  });

  it("records what the machine applied, and the heartbeat's hash, on the machine page", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    const hash = (await rowOf(w, first.configId))?.hash ?? "";
    const status = async () =>
      (await as(w.t, "admin").query(api.machineConfigs.getMachineConfig, { machineId: w.machine }))
        ?.configurationStatus;
    const badge = async () =>
      (await as(w.t, "admin").query(api.machineRegistry.listRegistryBadges, {})).find(
        (row) => row.machineId === w.machine,
      )?.configurationStatus;
    expect(await status()).toBe("pending");
    expect(await badge()).toBe("pending");

    const wrong = await machineFetch(w, key, "/api/machine/config/applied", { revision: 1, hash: "0".repeat(64) });
    expect(wrong.status).toBe(400);
    const unknown = await machineFetch(w, key, "/api/machine/config/applied", { revision: 9, hash });
    expect(unknown.status).toBe(400);
    for (const body of [{ revision: 1 }, { revision: 0, hash }, { revision: 1, hash: "A".repeat(64) }, { revision: 1, hash, refusal: "" }])
      expect((await machineFetch(w, key, "/api/machine/config/applied", body)).status).toBe(400);
    expect(await applyOnMachine(w, key, 1, hash)).toBe("applied");
    expect(await applyOnMachine(w, key, 1, hash)).toBe("unchanged");
    expect(await rowOf(w, first.configId)).toMatchObject({ status: "applied", appliedHash: hash });
    expect(await status()).toBe("mismatch");

    const beat = (body: unknown) => machineFetch(w, key, "/api/machine/heartbeat", body);
    expect((await beat({ config_hash: hash })).status).toBe(200);
    expect(await status()).toBe("applied");
    expect(await badge()).toBe("applied");
    const registry = await as(w.t, "admin").query(api.machineRegistry.getRegistry, { machineId: w.machine });
    expect(registry?.configurationStatus).toBe("applied");
    await beat({ config_hash: null });
    expect(await status()).toBe("mismatch");
    // A hash of another shape is not kept: the machine said none it could vouch for.
    await beat({ config_hash: hash });
    await beat({ config_hash: "not a hash" });
    expect(await status()).toBe("mismatch");
    expect((await w.t.run((ctx) => ctx.db.get(w.machine)))?.configHash).toBeUndefined();
    expect(await auditOf(w, first.configId)).toContainEqual({
      action: "machine_config.applied",
      fields: undefined,
      version: "1",
    });
  });

  it("records a refusal of the machine with its reason, and refuses a draft", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    const hash = (await rowOf(w, first.configId))?.hash ?? "";
    const draft = await propose(w, { ...BASE, location: "Brouillon" });
    const draftHash = (await rowOf(w, draft.configId))?.hash ?? "";
    expect(
      (await machineFetch(w, key, "/api/machine/config/applied", { revision: 2, hash: draftHash })).status,
    ).toBe(400);
    const refused = await machineFetch(w, key, "/api/machine/config/applied", {
      revision: 1,
      hash,
      refusal: "MOTOR_MAX_RPM: hors bornes",
    });
    expect(await refused.json()).toEqual({ status: "refused", server_contract_version: CONTRACT_VERSION });
    expect(await rowOf(w, first.configId)).toMatchObject({
      status: "rejected",
      rejectReason: "refusée par la machine : MOTOR_MAX_RPM: hors bornes",
    });
    const again = await machineFetch(w, key, "/api/machine/config/applied", { revision: 1, hash, refusal: "encore" });
    expect(await again.json()).toMatchObject({ status: "unchanged" });
    expect((await auditOf(w, first.configId)).map((entry) => entry.action)).toContain("machine_config.refused");
  });
});

describe("ANH-141 review R2: the two-admin rule rests on the site alone", () => {
  it("A1: never makes applied a revision rejected before any approval, whatever the machine says", async () => {
    const w = await seedWorld(modules);
    await setLevel(w, "auto");
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    const risky = await propose(w, { ...BASE, PROGRAMS_ENABLED: true });
    await asSecondAdmin(w).mutation(api.machineConfigs.rejectConfig, {
      configId: risky.configId,
      reason: "pas sans essai",
    });
    const seen = await as(w.t, "orgAdmin").query(api.machineConfigs.getMachineConfig, {
      machineId: w.machine,
    });
    const hash = seen?.revisions.find((row) => row.revision === risky.revision)?.hash ?? "";
    const ack = await machineFetch(w, key, "/api/machine/config/applied", {
      revision: risky.revision,
      hash,
    });
    expect(ack.status).toBe(400);
    expect((await rowOf(w, risky.configId))?.status).toBe("rejected");
    // The same safety value again, with a new place: still two admins.
    const next = await propose(w, { ...BASE, PROGRAMS_ENABLED: true, location: "Ailleurs" });
    await expect(
      as(w.t, "admin").mutation(api.machineConfigs.approveConfig, { configId: next.configId }),
    ).rejects.toThrow(/machine_config_self_approval/);
  });

  it("A2: a lowered level rejects an approved lock not yet applied, and says one applied", async () => {
    const w = await seedWorld(modules);
    await setLevel(w, "auto");
    const key = await machineKey(w);
    const first = await approvedFirst(w, { ...BASE, PROGRAMS_ENABLED: true });
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    const waiting = await propose(w, { ...BASE, PROGRAMS_ENABLED: true, MOTOR_MAX_RPM: 280 });
    await asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: waiting.configId });
    await as(w.t, "admin").mutation(api.machineRegistry.lowerValidationLevel, {
      machineId: w.machine,
      level: "bench",
      reason: "TEST nouvel essai requis",
    });
    expect(await rowOf(w, waiting.configId)).toMatchObject({
      status: "rejected",
      rejectReason: "machine validée jusqu'au niveau bench : PROGRAMS_ENABLED ne passe plus",
      rejectedBy: w.admin,
    });
    expect(await auditOf(w, waiting.configId)).toContainEqual({
      action: "machine_config.rejected",
      fields: ["PROGRAMS_ENABLED"],
      version: "2",
    });
    const view = await as(w.t, "admin").query(api.machineConfigs.getMachineConfig, {
      machineId: w.machine,
    });
    expect(view).toMatchObject({ inForceRevision: 1, locksAboveLevel: ["PROGRAMS_ENABLED"] });
  });

  it("A3: an author demoted since the proposal no longer counts as the first admin", async () => {
    const w = await seedWorld(modules);
    await addSecondAdmin(w);
    const first = await propose(w, BASE);
    await w.t.run(async (ctx) => {
      for (const membership of await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", w.admin))
        .collect())
        await ctx.db.patch(membership._id, { active: false });
    });
    const view = await asSecondAdmin(w).query(api.machineConfigs.getMachineConfig, {
      machineId: w.machine,
    });
    expect(view?.revisions[0].approveBlocked).toBe("machine_config_safety_needs_admins");
    await expect(
      asSecondAdmin(w).mutation(api.machineConfigs.approveConfig, { configId: first.configId }),
    ).rejects.toThrow(/machine_config_safety_needs_admins/);
  });
});

describe("ANH-141 reject", () => {
  it("rejects with a reason, the author's own draft for an organisation admin", async () => {
    const w = await seedWorld(modules);
    const key = await machineKey(w);
    const first = await approvedFirst(w);
    await applyOnMachine(w, key, 1, (await rowOf(w, first.configId))?.hash ?? "");
    const draft = await propose(w, { ...BASE, location: "Ailleurs" });
    await expect(
      as(w.t, "orgAdmin").mutation(api.machineConfigs.rejectConfig, { configId: draft.configId, reason: "non" }),
    ).rejects.toThrow(/not_authorized/);
    await expect(
      as(w.t, "admin").mutation(api.machineConfigs.rejectConfig, { configId: draft.configId, reason: " a " }),
    ).rejects.toThrow(/machine_config_reason_required/);
    await as(w.t, "admin").mutation(api.machineConfigs.rejectConfig, { configId: draft.configId, reason: " erreur de lieu " });
    expect(await rowOf(w, draft.configId)).toMatchObject({ status: "rejected", rejectReason: "erreur de lieu" });
    await expect(
      as(w.t, "admin").mutation(api.machineConfigs.rejectConfig, { configId: first.configId, reason: "trop tard" }),
    ).rejects.toThrow(/machine_config_wrong_status/);
    const own = await propose(w, { ...BASE, location: "Le mien" }, "orgAdmin");
    await as(w.t, "orgAdmin").mutation(api.machineConfigs.rejectConfig, { configId: own.configId, reason: "retire" });
    expect((await rowOf(w, own.configId))?.status).toBe("rejected");
  });
});
