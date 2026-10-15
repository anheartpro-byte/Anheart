/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import shippedContent from "../raspberry-pi/config/medical_parameters.json?raw";
import shippedPublicKey from "../raspberry-pi/config/medical.pub?raw";
import shippedSignatureUrl from "../raspberry-pi/config/medical_parameters.sig?url&inline";
import {
  compareMedicalVersions,
  MEDICAL_PUBLIC_KEY_PEM,
  medicalParametersStatus,
  parseMedicalDocument,
  verifyMedicalParametersSignature,
} from "./lib/medicalParameters";
import {
  addSession,
  as,
  backendOf,
  modules,
  NOW,
  registerShippedMedicalParameters,
  seedWorld,
  type World,
} from "./test.setup";

const shippedSignatureBase64 = /^data:[^,]*;base64,([A-Za-z0-9+/=]+)$/.exec(
  shippedSignatureUrl,
)?.[1];

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  delete process.env.PENDING_SESSION_TTL_S;
});

function parsedShippedDocument() {
  return JSON.parse(shippedContent) as {
    version: string;
    issued_at: string;
    signed_by: string;
    parameters: Record<string, unknown>;
  };
}

async function storedFiles(t: World["t"] | ReturnType<typeof backendOf>) {
  return await t.run((ctx) => ctx.db.system.query("_storage").collect());
}

async function insertFixtureVersion(
  t: World["t"] | ReturnType<typeof backendOf>,
  version: string,
  values: Partial<{
    zoneCeilingFraction: number;
    minRiderAge: number;
    hrMaxFormula: "tanaka";
  }> = {},
) {
  const storageId = await t.run((ctx) =>
    ctx.storage.store(new Blob([`fixture ${version}`])),
  );
  return await t.run((ctx) =>
    ctx.db.insert("medical_parameters_versions", {
      version,
      issuedAt: "2026-10-14",
      storageId,
      signedBy: "cle-de-test",
      signature: "fixture-signature",
      zoneCeilingFraction: values.zoneCeilingFraction ?? 0.9,
      minRiderAge: values.minRiderAge ?? 18,
      hrMaxFormula: values.hrMaxFormula ?? "tanaka",
      registeredAt: NOW,
    }),
  );
}

async function insertStoredVersion(
  t: World["t"] | ReturnType<typeof backendOf>,
  version: string,
  zoneCeilingFraction = 0.9,
) {
  const storageId = await t.run((ctx) =>
    ctx.storage.store(new Blob([`fixture ${version}`])),
  );
  const result = await t.mutation(internal.medicalParametersStore.insertVersion, {
    version,
    issuedAt: "2026-10-14",
    storageId,
    signedBy: "cle-de-test",
    signature: "fixture-signature",
    zoneCeilingFraction,
    minRiderAge: 18,
    hrMaxFormula: "tanaka",
  });
  if (typeof result !== "string") {
    await t.run((ctx) => ctx.storage.delete(storageId));
  }
  return result;
}

async function addArmablePendingAutoLaunch(w: World) {
  const sessionId = await addSession(w, {
    machineId: w.machine,
    userId: w.patient,
    startedById: w.manager,
    status: "pending",
    kind: "auto",
  });
  await w.t.run((ctx) =>
    ctx.db.patch(sessionId, {
      origin: "remote",
      profileId: w.profileId,
      profileName: "Programme p1",
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      totalDurationS: 600,
      subjectHrMax: 180,
      subjectAge: 36,
      subjectLabel: "patient Synthetic",
      operatorName: "manager Synthetic",
    }),
  );
  return sessionId;
}

async function rejectionLeavesNoWrites(
  content: string,
  signatureBase64: string,
) {
  const t = backendOf(modules);
  await expect(
    t.action(internal.medicalParameters.registerVersion, {
      content,
      signatureBase64,
    }),
  ).rejects.toThrow();
  expect(
    await t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
  ).toEqual([]);
  expect(await storedFiles(t)).toEqual([]);
}

describe("medical document parsing", () => {
  it("reads the shipped document and copies the Convex parameters", () => {
    expect(parseMedicalDocument(shippedContent)).toEqual({
      version: "0.1.0",
      issuedAt: "2026-10-14",
      signedBy: "cle-de-test",
      zoneCeilingFraction: 0.9,
      minRiderAge: 18,
      hrMaxFormula: "tanaka",
    });
  });

  it("rejects malformed JSON and duplicate keys at every object depth", () => {
    expect(() => parseMedicalDocument("{")).toThrow(/valid JSON/);
    expect(() => parseMedicalDocument("[]")).toThrow(/document must be a JSON object/);
    expect(() => parseMedicalDocument('["text"]')).toThrow(/document must be a JSON object/);
    expect(() => parseMedicalDocument("{}")).toThrow(/document.version is missing/);
    expect(() =>
      parseMedicalDocument('{"_comment":"unfinished}'),
    ).toThrow(/Unterminated JSON string/);
    const emptyParameters = { ...parsedShippedDocument(), parameters: {} };
    expect(() => parseMedicalDocument(JSON.stringify(emptyParameters))).toThrow(
      /document.parameters.hr_hard_max_bpm is missing/,
    );
    expect(() =>
      parseMedicalDocument(shippedContent.replace('"version": "0.1.0",', '"version": "0.1.0", "version": "0.1.0",')),
    ).toThrow(/Duplicate JSON member/);
    expect(() =>
      parseMedicalDocument(
        shippedContent.replace('"hr_critical_bpm": 158,', '"hr_critical_bpm": 158, "hr_critical_bpm": 158,'),
      ),
    ).toThrow(/Duplicate JSON member/);
  });

  it("rejects missing and unknown members throughout the document", () => {
    const missing = parsedShippedDocument();
    delete missing.parameters.hr_hard_max_bpm;
    expect(() => parseMedicalDocument(JSON.stringify(missing))).toThrow(/missing/);

    const unknown = parsedShippedDocument();
    unknown.parameters.extra = 1;
    expect(() => parseMedicalDocument(JSON.stringify(unknown))).toThrow(/unknown/);

    const unknownMotion = parsedShippedDocument();
    (unknownMotion.parameters.motion_limits as Record<string, unknown>).extra = 1;
    expect(() => parseMedicalDocument(JSON.stringify(unknownMotion))).toThrow(/unknown/);
  });

  it("checks signer, version, calendar date, and optional comment shape", () => {
    const badSigner = parsedShippedDocument();
    badSigner.signed_by = "Medical Team";
    expect(() => parseMedicalDocument(JSON.stringify(badSigner))).toThrow(/signer/);

    const badVersion = parsedShippedDocument();
    badVersion.version = "01.0.0";
    expect(() => parseMedicalDocument(JSON.stringify(badVersion))).toThrow(/MAJOR/);

    const badDate = parsedShippedDocument();
    badDate.issued_at = "2026-02-29";
    expect(() => parseMedicalDocument(JSON.stringify(badDate))).toThrow(/calendar date/);

    const badComment = { ...parsedShippedDocument(), _comment: false };
    expect(() => parseMedicalDocument(JSON.stringify(badComment))).toThrow(/_comment/);
  });

  it("checks positive numbers, physiological relationships, and console bounds", () => {
    const badWhole = parsedShippedDocument();
    badWhole.parameters.min_rider_age = true;
    expect(() => parseMedicalDocument(JSON.stringify(badWhole))).toThrow(/whole number/);

    const badFraction = parsedShippedDocument();
    badFraction.parameters.zone_ceiling_fraction = 1.1;
    expect(() => parseMedicalDocument(JSON.stringify(badFraction))).toThrow(/must not exceed/);

    const badFormula = parsedShippedDocument();
    badFormula.parameters.hr_max_formula = "unknown";
    expect(() => parseMedicalDocument(JSON.stringify(badFormula))).toThrow(/must be tanaka/);

    const badCritical = parsedShippedDocument();
    badCritical.parameters.hr_critical_bpm = 148;
    expect(() => parseMedicalDocument(JSON.stringify(badCritical))).toThrow(/must exceed/);

    const badAgeBound = parsedShippedDocument();
    badAgeBound.parameters.min_rider_age = 9;
    expect(() => parseMedicalDocument(JSON.stringify(badAgeBound))).toThrow(/10..100/);

    const badMotionBound = parsedShippedDocument();
    (badMotionBound.parameters.motion_limits as Record<string, unknown>).g_per_s = 0.6;
    expect(() => parseMedicalDocument(JSON.stringify(badMotionBound))).toThrow(/console bounds/);
  });

  it.each([
    ["hr_hard_max_bpm", "148"],
    ["hr_critical_bpm", "158"],
    ["min_rider_age", "18"],
    ["min_run_motor_rpm", "55"],
  ] as const)("requires integer JSON tokens for %s", (field, integerToken) => {
    const replaceToken = (token: string) =>
      shippedContent.replace(
        new RegExp(`("${field}"\\s*:\\s*)[0-9]+`),
        (_match, prefix: string) => `${prefix}${token}`,
      );
    expect(() => parseMedicalDocument(replaceToken(integerToken))).not.toThrow();
    for (const token of ["18.0", "18e0", "148.0", "1.48e2"]) {
      const content = replaceToken(token);
      expect(() => parseMedicalDocument(content), `${field}=${token}`).toThrow(
        /positive whole number/,
      );
    }
  });

  it("enforces the Pi console's inclusive heart-rate, age, and motion bounds", () => {
    const cases: Array<[string, Record<string, unknown>, RegExp]> = [
      ["hard maximum below", { hr_hard_max_bpm: 99 }, /heart-rate bounds/],
      ["hard maximum above", { hr_hard_max_bpm: 221, hr_critical_bpm: 222 }, /heart-rate bounds/],
      ["critical maximum below", { hr_hard_max_bpm: 98, hr_critical_bpm: 99 }, /heart-rate bounds/],
      ["critical maximum above", { hr_hard_max_bpm: 220, hr_critical_bpm: 221 }, /heart-rate bounds/],
      ["age above", { min_rider_age: 101 }, /10..100/],
    ];
    for (const [name, changes, expected] of cases) {
      const document = parsedShippedDocument();
      Object.assign(document.parameters, changes);
      expect(() => parseMedicalDocument(JSON.stringify(document)), name).toThrow(expected);
    }

    const motionCases: Array<[string, string, number]> = [
      ["output rate", "output_rpm_per_s", 2.1],
      ["g rate", "g_per_s", 0.6],
      ["minimum run", "min_run_motor_rpm", 201],
      ["maximum interval", "max_interval_s", 2.1],
    ];
    for (const [name, key, value] of motionCases) {
      const document = parsedShippedDocument();
      (document.parameters.motion_limits as Record<string, unknown>)[key] = value;
      expect(() => parseMedicalDocument(JSON.stringify(document)), name).toThrow(/console bounds/);
    }
  });

  it("accepts exact inclusive limits from the console configuration", () => {
    const document = parsedShippedDocument();
    Object.assign(document.parameters, {
      hr_hard_max_bpm: 100,
      hr_critical_bpm: 220,
      min_rider_age: 100,
      zone_ceiling_fraction: 1,
    });
    Object.assign(document.parameters.motion_limits as Record<string, unknown>, {
      output_rpm_per_s: 2,
      g_per_s: 0.5,
      min_run_motor_rpm: 200,
      max_interval_s: 2,
    });
    expect(() => parseMedicalDocument(JSON.stringify(document))).not.toThrow();
  });
});

describe("medical signatures and version registration", () => {
  it("pins the embedded Ed25519 key to the Pi key and verifies the exact shipped file bytes", async () => {
    if (!shippedSignatureBase64) throw new Error("The signature is not a base64 data URL");
    expect(MEDICAL_PUBLIC_KEY_PEM).toBe(shippedPublicKey);
    expect(
      await verifyMedicalParametersSignature(
        shippedContent,
        shippedSignatureBase64,
        MEDICAL_PUBLIC_KEY_PEM,
      ),
    ).toBe(true);
    const changedByte = new Uint8Array(new TextEncoder().encode(shippedContent));
    const changedAt = shippedContent.indexOf("0.1.0") + 4;
    changedByte[changedAt] = (changedByte[changedAt] ?? 0) ^ 1;
    const changedContent = new TextDecoder().decode(changedByte);
    expect(
      await verifyMedicalParametersSignature(
        changedContent,
        shippedSignatureBase64,
        MEDICAL_PUBLIC_KEY_PEM,
      ),
    ).toBe(false);
  });

  it("rejects an altered byte, absent signature, and a signature from another key without writing", async () => {
    if (!shippedSignatureBase64) throw new Error("The signature is not a base64 data URL");
    const changedByte = new Uint8Array(new TextEncoder().encode(shippedContent));
    const changedAt = shippedContent.indexOf("0.1.0") + 4;
    changedByte[changedAt] = (changedByte[changedAt] ?? 0) ^ 1;
    await rejectionLeavesNoWrites(
      new TextDecoder().decode(changedByte),
      shippedSignatureBase64,
    );
    await rejectionLeavesNoWrites(shippedContent, "");

    const foreignPair = await crypto.subtle.generateKey(
      { name: "Ed25519" },
      true,
      ["sign", "verify"],
    );
    const foreignSignature = await crypto.subtle.sign(
      { name: "Ed25519" },
      foreignPair.privateKey,
      new TextEncoder().encode(shippedContent),
    );
    const signatureBase64 = btoa(
      String.fromCharCode(...new Uint8Array(foreignSignature)),
    );
    await rejectionLeavesNoWrites(shippedContent, signatureBase64);
  });

  it("uses the supplied public key and refuses a different key", async () => {
    const pair = await crypto.subtle.generateKey(
      { name: "Ed25519" },
      true,
      ["sign", "verify"],
    );
    const spki = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
    const publicKeyPem = `-----BEGIN PUBLIC KEY-----\n${btoa(String.fromCharCode(...spki))}\n-----END PUBLIC KEY-----`;
    const signed = await crypto.subtle.sign(
      { name: "Ed25519" },
      pair.privateKey,
      new TextEncoder().encode(shippedContent),
    );
    const signatureBase64 = btoa(String.fromCharCode(...new Uint8Array(signed)));
    expect(
      await verifyMedicalParametersSignature(shippedContent, signatureBase64, publicKeyPem),
    ).toBe(true);
    expect(
      await verifyMedicalParametersSignature(
        shippedContent,
        signatureBase64,
        MEDICAL_PUBLIC_KEY_PEM,
      ),
    ).toBe(false);
    expect(
      await verifyMedicalParametersSignature(shippedContent, signatureBase64, "invalid key"),
    ).toBe(false);
    expect(
      await verifyMedicalParametersSignature(shippedContent, "not base64", publicKeyPem),
    ).toBe(false);
  });

  it("verifies UTF-8 comments with accents and rejects signatures of 63 or 65 bytes", async () => {
    const pair = await crypto.subtle.generateKey(
      { name: "Ed25519" },
      true,
      ["sign", "verify"],
    );
    const spki = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
    const publicKeyPem = `-----BEGIN PUBLIC KEY-----\n${btoa(String.fromCharCode(...spki))}\n-----END PUBLIC KEY-----`;
    const content = shippedContent.replace(
      /("_comment"\s*:\s*)"[^"]*"/,
      '$1"Paramètres médicaux signés"',
    );
    const signed = await crypto.subtle.sign(
      { name: "Ed25519" },
      pair.privateKey,
      new TextEncoder().encode(content),
    );
    const signature = new Uint8Array(signed);
    expect(parseMedicalDocument(content)).toMatchObject({ version: "0.1.0" });
    expect(
      await verifyMedicalParametersSignature(
        content,
        btoa(String.fromCharCode(...signature)),
        publicKeyPem,
      ),
    ).toBe(true);

    for (const length of [63, 65]) {
      const altered = new Uint8Array(length);
      altered.set(signature.slice(0, length));
      expect(
        await verifyMedicalParametersSignature(
          content,
          btoa(String.fromCharCode(...altered)),
          publicKeyPem,
        ),
      ).toBe(false);
    }
  });

  it("registers the shipped file and stores exactly its signed UTF-8 bytes", async () => {
    if (!shippedSignatureBase64) throw new Error("The signature is not a base64 data URL");
    const t = backendOf(modules);
    await registerShippedMedicalParameters(t);
    const rows = await t.run((ctx) => ctx.db.query("medical_parameters_versions").collect());
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      version: "0.1.0",
      issuedAt: "2026-10-14",
      signedBy: "cle-de-test",
      signature: shippedSignatureBase64,
      zoneCeilingFraction: 0.9,
      minRiderAge: 18,
      hrMaxFormula: "tanaka",
      registeredAt: NOW,
    });
    const storedBytes = await t.run(async (ctx) => {
      const stored = await ctx.storage.get(rows[0]!.storageId);
      return stored === null
        ? null
        : Array.from(new Uint8Array(await stored.arrayBuffer()));
    });
    expect(storedBytes).toEqual(Array.from(new TextEncoder().encode(shippedContent)));
  });

  it("rejects an already registered shipped version without storing it twice", async () => {
    if (!shippedSignatureBase64) throw new Error("The signature is not a base64 data URL");
    const t = backendOf(modules);
    await registerShippedMedicalParameters(t);
    await expect(registerShippedMedicalParameters(t)).rejects.toThrow(/already registered/);
    expect(
      await t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toHaveLength(1);
    expect(await storedFiles(t)).toHaveLength(1);
  });

  it("refuses registration while an unexpired launch without origin is pending", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "pending",
    });
    const versionsBefore = await w.t.run((ctx) =>
      ctx.db.query("medical_parameters_versions").collect(),
    );
    const filesBefore = await storedFiles(w.t);

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toEqual({
      refusal:
        "Cannot register a medical parameter version while 1 remote launch is pending; retry once it is armed, failed or expired (pending timeout: 300 s).",
    });

    expect(
      await w.t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toEqual(versionsBefore);
    expect(await storedFiles(w.t)).toEqual(filesBefore);
  });

  it("settles an expired launch during registration so a TTL increase cannot revive it", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addArmablePendingAutoLaunch(w);
    vi.setSystemTime(NOW + 300_001);

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toBe("0.2.0");

    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "failed",
      endedAt: NOW + 300_001,
      endCode: "expired_before_start",
      endReason: "expired before the machine armed it",
    });
    process.env.PENDING_SESSION_TTL_S = "600";
    expect(
      await w.t.query(internal.training.getPendingTrainingSession, {
        machineId: w.machine,
      }),
    ).toBeNull();
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({
      armed: false,
      code: "expired_before_start",
      message: "expired before the machine armed it",
    });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "failed",
      endCode: "expired_before_start",
    });
    expect(
      await w.t.run(async (ctx) =>
        (await ctx.db.system.query("_scheduled_functions").collect()).map(
          (job) => job.name,
        ),
      ),
    ).toContain("notifications:deliver");
    expect(
      await w.t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toHaveLength(2);
  });

  it("offers and arms the same unexpired launch fixture", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addArmablePendingAutoLaunch(w);

    await expect(
      w.t.query(internal.training.getPendingTrainingSession, {
        machineId: w.machine,
      }),
    ).resolves.toMatchObject({
      sessionId,
      profileId: w.profileId,
      subjectHrMax: 180,
      subjectId: w.patient,
    });
    await expect(
      w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).resolves.toEqual({ armed: true });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "active",
    });
  });

  it("registers a version without settling a pending legacy session", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
    });

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toBe("0.2.0");
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session).toMatchObject({ status: "pending" });
    expect(session?.kind).toBeUndefined();
    expect(
      await w.t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toHaveLength(2);
  });

  it("commits expired-launch failures while refusing registration for a live launch", async () => {
    const w = await seedWorld(modules);
    const expiredSessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });
    const liveSessionId = await addSession(w, {
      machineId: w.otherMachine,
      userId: w.otherPatient,
      status: "pending",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(expiredSessionId, { startedAt: NOW - 300_001 }),
    );

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toEqual({
      refusal:
        "Cannot register a medical parameter version while 1 remote launch is pending; retry once it is armed, failed or expired (pending timeout: 300 s).",
    });
    expect(await w.t.run((ctx) => ctx.db.get(expiredSessionId))).toMatchObject({
      status: "failed",
      endCode: "expired_before_start",
    });
    expect(await w.t.run((ctx) => ctx.db.get(liveSessionId))).toMatchObject({
      status: "pending",
    });
    expect(
      await w.t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toHaveLength(1);
  });

  it("refuses a shipped-file registration on an empty registry and cleans up its file", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      const versions = await ctx.db.query("medical_parameters_versions").collect();
      for (const version of versions) {
        await ctx.db.delete(version._id);
        await ctx.storage.delete(version.storageId);
      }
    });
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });

    await expect(registerShippedMedicalParameters(w.t)).rejects.toThrow(
      "Cannot register a medical parameter version while 1 remote launch is pending; retry once it is armed, failed or expired (pending timeout: 300 s).",
    );

    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "pending",
    });
    expect(
      await w.t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toEqual([]);
    expect(await storedFiles(w.t)).toEqual([]);
  });

  it("uses the plural refusal when two auto launches are pending", async () => {
    const w = await seedWorld(modules);
    await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });
    await addSession(w, {
      machineId: w.otherMachine,
      userId: w.otherPatient,
      status: "pending",
      kind: "auto",
    });

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toEqual({
      refusal:
        "Cannot register a medical parameter version while 2 remote launches are pending; retry once they are armed, failed or expired (pending timeout: 300 s).",
    });
  });

  it("counts a 400-second-old launch when the pending TTL is 600 seconds", async () => {
    process.env.PENDING_SESSION_TTL_S = "600";
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(sessionId, { startedAt: NOW - 400_000 }),
    );

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toEqual({
      refusal:
        "Cannot register a medical parameter version while 1 remote launch is pending; retry once it is armed, failed or expired (pending timeout: 600 s).",
    });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "pending",
      startedAt: NOW - 400_000,
    });
  });

  it("allows registration after a remote launch is already armed", async () => {
    const w = await seedWorld(modules);
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    await w.t.mutation(internal.training.markTrainingStarted, {
      machineId: w.machine,
      sessionId,
    });

    await expect(insertStoredVersion(w.t, "0.2.0")).resolves.toBe("0.2.0");
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "active",
    });
  });

  it("uses an accepted lower ceiling for new launches after registration", async () => {
    const w = await seedWorld(modules);
    await expect(insertStoredVersion(w.t, "0.2.0", 0.8)).resolves.toBe("0.2.0");

    await expect(
      as(w.t, "manager").mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
        userId: w.patient,
      }),
    ).rejects.toThrow(/zone_above_ceiling/);
  });

  it("rechecks duplicate and semver order in the insertion mutation", async () => {
    const t = backendOf(modules);
    await registerShippedMedicalParameters(t);
    const storageId = await t.run((ctx) =>
      ctx.storage.store(new Blob(["next signed file"])),
    );
    const insert = (version: string) =>
      t.mutation(internal.medicalParametersStore.insertVersion, {
        version,
        issuedAt: "2026-10-14",
        storageId,
        signedBy: "cle-de-test",
        signature: "fixture-signature",
        zoneCeilingFraction: 0.9,
        minRiderAge: 18,
        hrMaxFormula: "tanaka",
      });
    await insert("0.2.0");
    await expect(insert("0.2.0")).rejects.toThrow(/already registered/);
    await expect(insert("0.1.9")).rejects.toThrow(/not greater than current/);
    expect(
      await t.run((ctx) => ctx.db.query("medical_parameters_versions").collect()),
    ).toHaveLength(2);
  });

  it("compares numeric semver components and selects the greatest current version", async () => {
    expect(compareMedicalVersions("0.10.0", "0.2.0")).toBeGreaterThan(0);
    expect(compareMedicalVersions("1.0.0", "0.99.99")).toBeGreaterThan(0);
    const w = await seedWorld(modules);
    await insertFixtureVersion(w.t, "0.2.0");
    await insertFixtureVersion(w.t, "0.10.0");
    await expect(as(w.t, "manager").query(api.medicalParameters.current, {})).resolves.toMatchObject({
      version: "0.10.0",
    });
  });

  it("requires a signed-in user and returns null when no version is present", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("medical_parameters_versions").collect()) {
        await ctx.db.delete(row._id);
        await ctx.storage.delete(row.storageId);
      }
    });
    await expect(w.t.query(api.medicalParameters.current, {})).rejects.toThrow(/not_authenticated/);
    await expect(as(w.t, "patient").query(api.medicalParameters.current, {})).resolves.toBeNull();
  });
});

describe("current medical parameters in training", () => {
  it("classifies each reported medical version against the reference", () => {
    const cases = [
      [undefined, "0.1.0", "unknown"],
      [null, "0.1.0", "unsigned"],
      ["0.1.0", null, "no_reference"],
      ["0.1.0", "0.1.0", "current"],
      ["0.0.9", "0.1.0", "obsolete"],
      ["0.1.1", "0.1.0", "ahead"],
    ] as const;
    for (const [machineVersion, referenceVersion, expected] of cases) {
      expect(
        medicalParametersStatus(machineVersion, referenceVersion),
      ).toBe(expected);
    }
  });

  it("refuses an unsigned machine before checking whether its programmes are enabled", async () => {
    const w = await seedWorld(modules);
    const launchArgs = { machineId: w.machine, profileId: w.profileId, userId: w.patient };
    const sessionsBefore = await w.t.run((ctx) =>
      ctx.db.query("sessions").collect(),
    );
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, {
        medicalParametersVersion: null,
        programsEnabled: false,
      }),
    );
    await expect(
      as(w.t, "manager").mutation(api.training.launchAutoSession, launchArgs),
    ).rejects.toThrow(/medical_parameters_unsigned/);
    expect(
      await w.t.run((ctx) => ctx.db.query("sessions").collect()),
    ).toEqual(sessionsBefore);
  });

  it("does not add a refusal when the machine has not announced a medical version", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { medicalParametersVersion: undefined }),
    );
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "pending",
      kind: "auto",
    });
  });

  it("uses a stored fraction and minimum age for remote launch refusals", async () => {
    const w = await seedWorld(modules);
    await insertFixtureVersion(w.t, "0.2.0", { zoneCeilingFraction: 0.8 });
    const zoneRefusal = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    ).then(
      () => null,
      (error: unknown) => (error instanceof Error ? error.message : String(error)),
    );
    expect(zoneRefusal).toBe("zone_above_ceiling");

    await insertFixtureVersion(w.t, "0.3.0", { zoneCeilingFraction: 0.9, minRiderAge: 37 });
    const ageRefusal = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    ).then(
      () => null,
      (error: unknown) => (error instanceof Error ? error.message : String(error)),
    );
    expect(ageRefusal).toBe("rider_too_young");
  });

  it("uses the current minimum again when a pending launch is armed", async () => {
    const w = await seedWorld(modules);
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    await insertFixtureVersion(w.t, "0.2.0", { minRiderAge: 37 });
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toMatchObject({ armed: false, code: "age_refused_before_start" });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "failed",
      endCode: "age_refused_before_start",
    });
  });

  it("fails closed in launch order and returns age refusal from arming if the registry is absent", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("medical_parameters_versions").collect()) {
        await ctx.db.delete(row._id);
        await ctx.storage.delete(row.storageId);
      }
      await ctx.db.patch(w.machine, {
        status: "offline",
        programsEnabled: false,
        medicalParametersVersion: null,
      });
    });
    const launchArgs = { machineId: w.machine, profileId: w.profileId, userId: w.patient };
    await expect(as(w.t, "manager").mutation(api.training.launchAutoSession, launchArgs)).rejects.toThrow(/machine_offline/);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { status: "online" }));
    await expect(as(w.t, "manager").mutation(api.training.launchAutoSession, launchArgs)).rejects.toThrow(/medical_parameters_unavailable/);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { programsEnabled: true }));
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      startedById: w.manager,
      status: "pending",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(sessionId, {
        origin: "remote",
        profileId: w.profileId,
        profileName: "Programme p1",
        zoneLowBpm: 130,
        zoneHighBpm: 150,
        totalDurationS: 600,
        subjectHrMax: 180,
        subjectAge: 36,
        subjectLabel: "patient Synthetic",
        operatorName: "manager Synthetic",
      }),
    );
    expect(
      await w.t.query(internal.training.getPendingTrainingSession, { machineId: w.machine }),
    ).toBeNull();
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toMatchObject({ armed: false, code: "age_refused_before_start" });
    const polledWithoutVersion = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(polledWithoutVersion).toMatchObject({ endCode: "age_refused_before_start" });
    const sweepSessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      startedById: w.manager,
      status: "pending",
      kind: "auto",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(sweepSessionId, {
        origin: "remote",
        profileId: w.profileId,
        subjectHrMax: 180,
      }),
    );
    await expect(w.t.mutation(internal.training.sweepPendingSessions, {})).resolves.toEqual({
      failed: 1,
    });
    await expect(w.t.run((ctx) => ctx.db.get(sweepSessionId))).resolves.toMatchObject({
      endCode: "age_refused_before_start",
    });
  });

  it("does not use estimated heart rate without a version and preserves measured values", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("medical_parameters_versions").collect()) {
        await ctx.db.delete(row._id);
        await ctx.storage.delete(row.storageId);
      }
    });
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: undefined, birthYear: 1990 }));
    await expect(as(w.t, "manager").query(api.users.getUserById, { userId: w.patient })).resolves.toMatchObject({ effectiveHrMax: null });
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 180 }));
    await expect(as(w.t, "manager").query(api.users.getUserById, { userId: w.patient })).resolves.toMatchObject({ effectiveHrMax: 180 });
  });
});
