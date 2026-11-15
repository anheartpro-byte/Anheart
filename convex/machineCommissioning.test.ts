/// <reference types="vite/client" />
/**
 * ANH-115 EX-4 and EX-5: the commissioning of a machine (contract 1.18). The
 * report a console sends, checked to the contract; the comparison with the
 * registry, a drive value at a tenth; the record written once, signed on the
 * catalogue's channel, with its line in the logbook and its audit; what is
 * refused with nothing written; the sheet's typed values. Who may read the
 * records, cell by cell, is `authorization.matrix.ts`.
 *
 * Every key here is made at run time: no secret is written anywhere.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { CATALOG_SIGNING_KEY_ENV } from "./lib/programmes";
import { OPERATOR_CODE } from "./lib/consoleAccess";
import {
  COMMISSIONING_KIND,
  COMMISSIONING_REFUSALS,
  DRIVE_FIELDS,
  commissioningPayload,
  findingsOf,
  parseCommissioningReport,
  sameReference,
  sheetParametersRefusal,
  tenthsOf,
  verdictOf,
  type CommissioningReference,
  type CommissioningReport,
  type DriveParameters,
} from "./lib/commissioning";
import { registryUpload } from "./machineRegistry.fixtures";
import { as, modules, seedWorld, type World } from "./test.setup";
import machineApi from "../contracts/machine-api.json";

const SECTION = machineApi.machine_commissioning;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(Date.UTC(2026, 10, 15, 9));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
});

const SHEET: DriveParameters = { tfrHz: 60, hspHz: 50, lspHz: 0, accS: 3, decS: 3 };
const ADDRESS = "20:16:07:18:15:52";

/** A report every check passes, from `operator`, attested by `attester`. */
function report(operator: string, attester = operator): CommissioningReport {
  return {
    ranAt: Date.UTC(2026, 10, 15, 8, 59),
    operator,
    attestation: {
      operator: attester,
      statement: "a latching mushroom emergency stop is wired normally-closed into P24 -> STO",
      at: Date.UTC(2026, 10, 15, 8, 50),
    },
    stoSource: "drive_input",
    controls: [
      { id: "drive_link", state: "ok", detail: "variateur lu il y a 0.4 s" },
      { id: "temperature", state: "warn", detail: "temperature du Pi non mesuree" },
    ],
    drive: { ...SHEET },
    bitalinoAddress: ADDRESS.toUpperCase(),
    softwareVersion: "pi-0.4.0",
    medicalParametersVersion: "med-2026.1",
    configHash: "a".repeat(64),
  };
}

const REFERENCE: CommissioningReference = {
  sheet: { version: "v3", signedBy: "TEST signataire", at: Date.UTC(2026, 9, 1), parameters: SHEET },
  bitalinoAddress: ADDRESS,
  expectedSoftwareVersion: "pi-0.4.0",
};

describe("ANH-115 EX-4 the report, checked to the contract", () => {
  it("keeps the contract's drive fields and finding ids", () => {
    expect([...DRIVE_FIELDS]).toEqual(SECTION.drive_fields);
    expect(COMMISSIONING_KIND).toBe("commissioning_record");
    const ids = findingsOf(
      {
        ...report("u"),
        stoSource: "sim_ready",
        drive: { tfrHz: 1, hspHz: 1, lspHz: 1, accS: 1, decS: 1 },
        bitalinoAddress: null,
        controls: [{ id: "estop", state: "block", detail: "non atteste" }],
      },
      { ...REFERENCE, expectedSoftwareVersion: "pi-9.9.9" },
    ).map((f) => f.id);
    for (const id of ids) {
      if (!id.startsWith("control:")) expect(SECTION.finding_ids).toContain(id);
    }
    expect(SECTION.finding_ids).toContain("sto_bridged");
    expect(SECTION.finding_ids).toContain("drive_unread");
  });

  it("takes a report of the contract's shape", () => {
    expect(parseCommissioningReport(report("u"))).toEqual(report("u"));
    expect(parseCommissioningReport({ ...report("u"), drive: null })?.drive).toBeNull();
  });

  const bad: Array<[string, (r: CommissioningReport) => unknown]> = [
    ["a field the contract does not name", (r) => ({ ...r, extra: 1 })],
    ["no controls", (r) => ({ ...r, controls: [] })],
    ["too many controls", (r) => ({
      ...r,
      controls: Array.from({ length: SECTION.max_controls + 1 }, (_, n) => ({ id: `c${n}`, state: "ok", detail: "x" })),
    })],
    ["two controls of one id", (r) => ({ ...r, controls: [r.controls[0], r.controls[0]] })],
    ["a state the contract does not name", (r) => ({ ...r, controls: [{ ...r.controls[0], state: "pending" }] })],
    ["an id that is not a token", (r) => ({ ...r, controls: [{ ...r.controls[0], id: "Drive link" }] })],
    ["a detail with a control character", (r) => ({ ...r, controls: [{ ...r.controls[0], detail: "a\u0007b" }] })],
    ["a detail too long", (r) => ({ ...r, controls: [{ ...r.controls[0], detail: "x".repeat(SECTION.max_detail_chars + 1) }] })],
    ["a detail with a space at its end", (r) => ({ ...r, controls: [{ ...r.controls[0], detail: "x " }] })],
    ["a drive value not a tenth", (r) => ({ ...r, drive: { ...SHEET, hspHz: 50.05 } })],
    ["a drive value above the register", (r) => ({ ...r, drive: { ...SHEET, accS: SECTION.drive_read_max + 0.1 } })],
    ["a negative drive value", (r) => ({ ...r, drive: { ...SHEET, lspHz: -0.1 } })],
    ["a drive field missing", (r) => ({ ...r, drive: { tfrHz: 60, hspHz: 50, lspHz: 0, accS: 3 } })],
    ["an STO source the contract does not name", (r) => ({ ...r, stoSource: "jumper" })],
    ["a date that is not whole", (r) => ({ ...r, ranAt: 1.5 })],
    ["a statement empty", (r) => ({ ...r, attestation: { ...r.attestation, statement: "" } })],
    ["an attestation without its author", (r) => ({ ...r, attestation: { statement: "x", at: 1 } })],
    ["a version too long", (r) => ({ ...r, softwareVersion: "v".repeat(SECTION.max_text_chars + 1) })],
    ["a config hash of another form", (r) => ({ ...r, configHash: "ABC" })],
    ["not an object", () => [1, 2]],
  ];
  it.each(bad)("refuses %s", (_name, change) => {
    expect(parseCommissioningReport(change(report("u")))).toBeNull();
  });
});

describe("ANH-115 EX-4 the comparison with the registry", () => {
  it("finds nothing when everything agrees, the address in another case", () => {
    expect(findingsOf(report("u"), REFERENCE)).toEqual([]);
    expect(verdictOf([])).toBe("conforming");
  });

  it("names each drive value that differs by a tenth, expected and read", () => {
    const findings = findingsOf(
      { ...report("u"), drive: { tfrHz: 60, hspHz: 50.1, lspHz: 0, accS: 3, decS: 2.9 } },
      REFERENCE,
    );
    expect(findings).toEqual([
      { id: "drive_hsp", expected: "50.0 Hz", read: "50.1 Hz" },
      { id: "drive_dec", expected: "3.0 s", read: "2.9 s" },
    ]);
    expect(verdictOf(findings)).toBe("nonconforming");
  });

  it("says the drive unread, a bridged or a simulated STO, and each red control", () => {
    const findings = findingsOf(
      {
        ...report("u"),
        stoSource: "bridged_declared",
        drive: null,
        controls: [
          { id: "estop", state: "block", detail: "arret d'urgence non atteste" },
          { id: "cloud", state: "warn", detail: "tableau de bord injoignable" },
        ],
      },
      REFERENCE,
    );
    expect(findings).toEqual([
      { id: "sto_bridged", expected: "drive_input", read: "bridged_declared" },
      { id: "control:estop", expected: "ok", read: "arret d'urgence non atteste" },
      { id: "drive_unread", expected: "lus", read: "non lus" },
    ]);
    expect(findingsOf({ ...report("u"), stoSource: "sim_ready" }, REFERENCE)).toEqual([
      { id: "sto_source", expected: "drive_input", read: "sim_ready" },
    ]);
  });

  it("holds an address or a version the registry does not give as a finding", () => {
    const loose = { ...REFERENCE, bitalinoAddress: null, expectedSoftwareVersion: null };
    expect(findingsOf({ ...report("u"), bitalinoAddress: null }, loose)).toEqual([
      { id: "bitalino_address", expected: "non renseignée au registre", read: "non lue" },
      { id: "software_version", expected: "non renseignée au registre", read: "pi-0.4.0" },
    ]);
    expect(
      findingsOf(
        { ...report("u"), bitalinoAddress: "00:00:00:00:00:01", softwareVersion: "pi-0.3.9" },
        REFERENCE,
      ),
    ).toEqual([
      { id: "bitalino_address", expected: ADDRESS, read: "00:00:00:00:00:01" },
      { id: "software_version", expected: "pi-0.4.0", read: "pi-0.3.9" },
    ]);
  });

  it("reads a value as tenths, or not at all", () => {
    expect(tenthsOf(50)).toBe(500);
    expect(tenthsOf(0.1 + 0.2)).toBe(3);
    expect(tenthsOf(50.05)).toBeNull();
    expect(tenthsOf(Number.NaN)).toBeNull();
    expect(tenthsOf(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("compares two references whatever their keys' order", () => {
    const reordered = {
      expectedSoftwareVersion: REFERENCE.expectedSoftwareVersion,
      bitalinoAddress: REFERENCE.bitalinoAddress,
      sheet: { parameters: { ...SHEET }, at: REFERENCE.sheet.at, signedBy: "TEST signataire", version: "v3" },
    };
    expect(sameReference(REFERENCE, reordered)).toBe(true);
    expect(sameReference(REFERENCE, { ...REFERENCE, sheet: { ...REFERENCE.sheet, version: "v4" } })).toBe(false);
  });

  it("writes one canonical text, its kind first", () => {
    const text = commissioningPayload({
      machineId: "m1",
      recordedAt: 5,
      verdict: "conforming",
      findings: [],
      reference: REFERENCE,
      report: { ...report("u"), drive: null },
    });
    const parsed = JSON.parse(text) as Record<string, unknown>;
    expect(Object.keys(parsed)).toEqual(["kind", "machineId", "recordedAt", "verdict", "findings", "reference", "report"]);
    expect(parsed.kind).toBe(COMMISSIONING_KIND);
    expect((parsed.report as { drive: unknown }).drive).toBeNull();
  });

  it("holds the sheet's typed values to the keypad's ranges, in tenths, LSP <= HSP <= tFr", () => {
    expect(sheetParametersRefusal(SHEET)).toBeNull();
    expect(sheetParametersRefusal({ ...SHEET, tfrHz: 9.9 })).toBe("tfrHz");
    expect(sheetParametersRefusal({ ...SHEET, tfrHz: 599.1 })).toBe("tfrHz");
    expect(sheetParametersRefusal({ ...SHEET, accS: 1000 })).toBe("accS");
    expect(sheetParametersRefusal({ ...SHEET, decS: -0.1 })).toBe("decS");
    expect(sheetParametersRefusal({ ...SHEET, lspHz: 0.05 })).toBe("lspHz");
    expect(sheetParametersRefusal({ ...SHEET, hspHz: 61 })).toBe("hspHz");
    expect(sheetParametersRefusal({ ...SHEET, lspHz: 51 })).toBe("lspHz");
  });
});

// ---------------------------------------------------------------------------
// The route, on a seeded world
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

async function machineKey(w: World) {
  return (await as(w.t, "admin").mutation(api.machineKeys.createMachineKey, { machineId: w.machine })).apiKey;
}

/** A synthetic console code: the shape the list carries, made of plain words. */
const CODE = {
  algorithm: "scrypt" as const,
  n: OPERATOR_CODE.n,
  r: OPERATOR_CODE.r,
  p: OPERATOR_CODE.p,
  salt: btoa("sel de test"),
  hash: btoa("empreinte de test"),
};

async function enableOperator(w: World, userId: Id<"users">, withCode = true) {
  await w.t.run((ctx) =>
    ctx.db.insert("machine_operators", {
      organizationId: w.orgA,
      machineId: w.machine,
      userId,
      operatorCode: withCode ? CODE : undefined,
      codeSetAt: withCode ? Date.now() : undefined,
      enabledAt: Date.now(),
      enabledBy: w.orgAdmin,
    }),
  );
}

/** The machine's registry with a drive sheet, its values typed unless `parameters` is null. */
async function withRegistry(w: World, parameters: DriveParameters | null = SHEET) {
  const storageId = await registryUpload(w);
  await w.t.run((ctx) =>
    ctx.db.insert("machine_registry", {
      organizationId: w.orgA,
      machineId: w.machine,
      contacts: [],
      bitalinoAddress: ADDRESS,
      expectedSoftwareVersion: "pi-0.4.0",
      driveParameterSheet: {
        storageId,
        contentType: "application/pdf",
        version: "v3",
        signedBy: "TEST signataire",
        at: Date.UTC(2026, 9, 1),
        attachedBy: w.admin,
        attachedAt: Date.now(),
        parameters: parameters ?? undefined,
      },
      driveParameterSheetHistory: [],
      validation: { level: "none", reviews: [], downgrades: [] },
      updatedAt: Date.now(),
      updatedBy: w.admin,
    }),
  );
}

async function ready(w: World) {
  await withRegistry(w);
  await enableOperator(w, w.manager);
  return await machineKey(w);
}

function send(w: World, key: string, body: unknown) {
  vi.setSystemTime(Date.now() + 30_001);
  return w.t.fetch("/api/machine/commissioning", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
      "Content-Type": "application/json",
    },
    body: typeof body === "string" ? body : JSON.stringify(body),
  });
}

async function written(w: World) {
  return await w.t.run(async (ctx) => ({
    records: await ctx.db.query("machine_commissioning").collect(),
    logbook: (await ctx.db.query("machine_logbook").collect()).filter((e) => e.kind === "commissioning"),
    audit: (await ctx.db.query("audit_log").collect()).filter((e) => e.action === "commissioning.recorded"),
    registry: await ctx.db
      .query("machine_registry")
      .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
      .unique(),
  }));
}

describe("ANH-115 EX-5 a commissioning recorded", () => {
  it("records a conforming one, signed, its line in the logbook, the level still none", async () => {
    const w = await seedWorld(modules);
    const signing = await keyPair();
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, signing.privateBase64);
    const key = await ready(w);
    const response = await send(w, key, report(w.manager));
    expect(response.status).toBe(200);
    const answer = (await response.json()) as Record<string, unknown>;
    expect(Object.keys(answer)).toEqual(SECTION.response_fields);
    expect(answer).toMatchObject({ verdict: "conforming", findings: [], signed: true });
    const { records, logbook, audit, registry } = await written(w);
    expect(records).toHaveLength(1);
    const [row] = records;
    expect(row._id).toBe(answer.recordId);
    expect(row).toMatchObject({ machineId: w.machine, organizationId: w.orgA, operator: w.manager, attestedBy: w.manager });
    const verified = await crypto.subtle.verify(
      { name: "Ed25519" },
      signing.publicKey,
      unbase64(row.signature as string),
      new TextEncoder().encode(row.payload),
    );
    expect(verified).toBe(true);
    const text = JSON.parse(row.payload) as { kind: string; machineId: string; reference: CommissioningReference };
    expect(text.kind).toBe(COMMISSIONING_KIND);
    expect(text.machineId).toBe(w.machine);
    expect(sameReference(text.reference, REFERENCE)).toBe(true);
    expect(logbook).toHaveLength(1);
    expect(logbook[0]).toMatchObject({
      summary: "mise en service conforme, fiche variateur v3",
      refs: { softwareVersion: "pi-0.4.0" },
    });
    expect(logbook[0].by).toBeUndefined();
    expect(logbook[0].details).toBeUndefined();
    expect(audit).toHaveLength(1);
    expect(audit[0]).toMatchObject({ actorRole: "machine", targetId: w.machine, organizationId: w.orgA });
    expect(registry?.validation.level).toBe("none");
  });

  it("records a red run as evidence, nonconforming, unsigned without a key", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    await enableOperator(w, w.otherManager);
    const run = {
      ...report(w.manager, w.otherManager),
      stoSource: "bridged_declared",
      controls: [{ id: "sto_ready", state: "block", detail: "chaine STO pontee (declaree)" }],
    };
    const response = await send(w, key, run);
    expect(response.status).toBe(200);
    const answer = (await response.json()) as { verdict: string; signed: boolean; findings: { id: string }[] };
    expect(answer.verdict).toBe("nonconforming");
    expect(answer.signed).toBe(false);
    expect(answer.findings.map((f) => f.id)).toEqual(["sto_bridged", "control:sto_ready"]);
    const { records, logbook } = await written(w);
    expect(records[0]).toMatchObject({ signature: null, attestedBy: w.otherManager, verdict: "nonconforming" });
    expect(logbook[0].summary).toBe("mise en service non conforme : 2 écart(s), fiche variateur v3");
    expect(logbook[0].details).toBe(
      "sto_bridged : attendu drive_input, lu bridged_declared\ncontrol:sto_ready : attendu ok, lu chaine STO pontee (declaree)",
    );
  });

  it("logs a key that is not Ed25519, and records unsigned", async () => {
    const w = await seedWorld(modules);
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, btoa("pas une clé"));
    const errors = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const key = await ready(w);
    const response = await send(w, key, report(w.manager));
    expect(((await response.json()) as { signed: boolean }).signed).toBe(false);
    expect(errors).toHaveBeenCalledWith(`${CATALOG_SIGNING_KEY_ENV} is set but is not an Ed25519 private key`);
    errors.mockRestore();
  });
});

describe("ANH-115 EX-5 what is refused, nothing written", () => {
  async function refusedWith(w: World, key: string, body: unknown, message: string) {
    const response = await send(w, key, body);
    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({ error: "commissioning_refused", message });
    const { records, logbook, audit } = await written(w);
    expect([records.length, logbook.length, audit.length]).toEqual([0, 0, 0]);
  }

  it("refuses a machine without a registry, or with a sheet without its values", async () => {
    const w = await seedWorld(modules);
    await enableOperator(w, w.manager);
    const key = await machineKey(w);
    await refusedWith(w, key, report(w.manager), COMMISSIONING_REFUSALS.noSheet);
    await withRegistry(w, null);
    await refusedWith(w, key, report(w.manager), COMMISSIONING_REFUSALS.noSheet);
  });

  it("refuses an operator not on the list: unknown, without a code, disabled, or not of the organisation", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    await refusedWith(w, key, report("not-an-id"), COMMISSIONING_REFUSALS.operator);
    await refusedWith(w, key, report(w.otherManager, w.manager), COMMISSIONING_REFUSALS.operator);
    await enableOperator(w, w.otherManager, false);
    await refusedWith(w, key, report(w.otherManager, w.manager), COMMISSIONING_REFUSALS.operator);
    await enableOperator(w, w.orgBManager);
    await refusedWith(w, key, report(w.orgBManager, w.manager), COMMISSIONING_REFUSALS.operator);
    await w.t.run(async (ctx) => {
      for (const row of await ctx.db.query("machine_operators").collect())
        if (row.userId === w.manager) await ctx.db.patch(row._id, { disabledAt: Date.now() });
    });
    await refusedWith(w, key, report(w.manager), COMMISSIONING_REFUSALS.operator);
  });

  it("refuses an attestation whose author is not on the list", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    await refusedWith(w, key, report(w.manager, w.patient), COMMISSIONING_REFUSALS.attester);
    await refusedWith(w, key, report(w.manager, "x"), COMMISSIONING_REFUSALS.attester);
  });

  it("refuses a record whose registry changed after it was compared", async () => {
    const w = await seedWorld(modules);
    const record = (reference: CommissioningReference) =>
      w.t.mutation(internal.machineCommissioning.record, {
        machineId: w.machine,
        operator: w.manager,
        attestedBy: w.manager,
        reference,
        recordedAt: Date.now(),
        verdict: "conforming",
        findings: [],
        payload: "{}",
        signature: null,
        softwareVersion: "pi-0.4.0",
      });
    // The sheet removed in between, then replaced by another version.
    expect(await record(REFERENCE)).toEqual({ status: "refused", message: COMMISSIONING_REFUSALS.noSheet });
    await ready(w);
    const outcome = await record({ ...REFERENCE, sheet: { ...REFERENCE.sheet, version: "v2" } });
    expect(outcome).toEqual({ status: "refused", message: COMMISSIONING_REFUSALS.changed });
    const { records, logbook, audit } = await written(w);
    expect([records.length, logbook.length, audit.length]).toEqual([0, 0, 0]);
  });

  it("answers a malformed body 400, a body too big 413, a second within 30 s 429", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    const malformed = await send(w, key, { ...report(w.manager), stoSource: "jumper" });
    expect(malformed.status).toBe(400);
    expect(((await malformed.json()) as { error: string }).error).toBe("invalid_request");
    expect((await send(w, key, "not json")).status).toBe(400);
    const big = await send(w, key, "x".repeat(machineApi.max_body_bytes.routes["POST /api/machine/commissioning"] + 1));
    expect(big.status).toBe(413);
    vi.setSystemTime(Date.now() + 60_001);
    const call = () =>
      w.t.fetch("/api/machine/commissioning", {
        method: "POST",
        headers: { Authorization: `Bearer ${key}`, [CONTRACT_HEADER]: CONTRACT_VERSION },
        body: JSON.stringify(report(w.manager)),
      });
    expect((await call()).status).toBe(200);
    expect((await call()).status).toBe(200);
    expect((await call()).status).toBe(429);
  });
});

describe("ANH-115 EX-5 the records, read", () => {
  it("lists a machine's records, most recent first, and gives one with its names", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    await send(w, key, { ...report(w.manager), drive: null });
    await send(w, key, report(w.manager));
    const listed = await as(w.t, "admin").query(api.machineCommissioning.listCommissionings, { machineId: w.machine });
    expect(listed.map((r) => [r.verdict, r.findings, r.signed])).toEqual([
      ["conforming", 0, false],
      ["nonconforming", 1, false],
    ]);
    const record = await as(w.t, "orgAdmin").query(api.machineCommissioning.getCommissioning, {
      recordId: listed[1].recordId,
    });
    expect(record).toMatchObject({
      machineId: w.machine,
      verdict: "nonconforming",
      findings: [{ id: "drive_unread", expected: "lus", read: "non lus" }],
      operator: { userId: w.manager, firstName: "manager", lastName: "Synthetic" },
      attestedBy: { userId: w.manager },
      report: { stoSource: "drive_input", drive: null, statement: report("u").attestation.statement },
      signature: null,
    });
    expect(record?.payload).toContain(`"kind":"${COMMISSIONING_KIND}"`);
    expect(await as(w.t, "manager").query(api.machineCommissioning.listCommissionings, { machineId: w.machine })).toEqual([]);
    expect(
      await as(w.t, "manager").query(api.machineCommissioning.getCommissioning, { recordId: listed[0].recordId }),
    ).toBeNull();
  });

  it("names a deleted account by its id only", async () => {
    const w = await seedWorld(modules);
    const key = await ready(w);
    await send(w, key, report(w.manager));
    await w.t.run((ctx) => ctx.db.patch(w.manager, { clerkDeletedAt: Date.now() }));
    const [listed] = await as(w.t, "admin").query(api.machineCommissioning.listCommissionings, { machineId: w.machine });
    const record = await as(w.t, "admin").query(api.machineCommissioning.getCommissioning, { recordId: listed.recordId });
    expect(record?.operator).toEqual({ userId: w.manager });
  });
});

describe("ANH-115 EX-5 the drive sheet's typed values", () => {
  it("stores the values typed with a sheet, and refuses values outside the ranges", async () => {
    const w = await seedWorld(modules);
    const attach = async (parameters?: DriveParameters) =>
      as(w.t, "admin").mutation(api.machineRegistry.attachDriveParameterSheet, {
        machineId: w.machine,
        storageId: await registryUpload(w),
        version: "v4",
        signedBy: "TEST signataire",
        at: Date.now(),
        parameters,
      });
    await attach(SHEET);
    let row = (await written(w)).registry;
    expect(row?.driveParameterSheet?.parameters).toEqual(SHEET);
    await attach();
    row = (await written(w)).registry;
    expect(row?.driveParameterSheet?.parameters).toBeUndefined();
    expect(row?.driveParameterSheetHistory[0].parameters).toEqual(SHEET);
    await expect(attach({ ...SHEET, hspHz: 70 })).rejects.toThrow(/invalid_drive_parameters/);
    await expect(attach({ ...SHEET, accS: 3.05 })).rejects.toThrow(/invalid_drive_parameters/);
  });
});
