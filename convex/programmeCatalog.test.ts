/// <reference types="vite/client" />
/**
 * ANH-138: the signed catalogue the machines fetch (`GET /api/machine/programmes`,
 * `programme_catalog` of contracts/machine-api.json) and what a machine says
 * of it with its programmes (`POST /api/machine/profiles`, contract 1.11).
 *
 * Every key here is generated at run time and forgotten with the test: no
 * private key is written anywhere, in the repository or out of it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { verifyMedicalParametersSignature } from "./lib/medicalParameters";
import {
  CATALOG_SIGNING_KEY_ENV,
  catalogRevision,
  PROGRAMME_MEDICAL_VERSION,
  PROGRAMME_SCHEMA_VERSION,
  type CatalogRow,
} from "./lib/programmes";
import { JOG } from "./programmes.fixtures";
import { modules, NOW, seedMachineWorld } from "./test.setup";
import machineApi from "../contracts/machine-api.json";
import shippedDefaults from "../raspberry-pi/config/profiles.default.json";

type World = Awaited<ReturnType<typeof seedMachineWorld>>;

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

const base64 = (bytes: ArrayBuffer) => btoa(String.fromCharCode(...new Uint8Array(bytes)));
const armoured = (label: string, body: string) =>
  [`-----BEGIN ${label}-----`, body, `-----END ${label}-----`].join("\n");

/** A key pair made now: the private half in PEM for Convex, the public half in PEM. */
async function keyPair() {
  const pair = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  const pkcs8 = base64(await crypto.subtle.exportKey("pkcs8", pair.privateKey));
  return {
    privatePem: armoured("PRIVATE KEY", pkcs8),
    privateBase64: pkcs8,
    publicPem: armoured("PUBLIC KEY", base64(await crypto.subtle.exportKey("spki", pair.publicKey))),
  };
}

async function programme(
  w: World,
  status: Doc<"programmes">["status"],
  profile: Partial<typeof JOG>,
  organizationId: Id<"organizations"> | null = null,
) {
  await w.t.run((ctx) =>
    ctx.db.insert("programmes", {
      ...JOG,
      ...profile,
      organizationId,
      status,
      schemaVersion: PROGRAMME_SCHEMA_VERSION,
      revision: status === "published" || status === "retired" ? 1 : 0,
      medicalParametersVersion: PROGRAMME_MEDICAL_VERSION,
      medicalSignoff: null,
      simulationReport: null,
      validatedBy: null,
      createdBy: w.adminId,
      updatedBy: w.adminId,
      createdAt: NOW,
      updatedAt: NOW,
    }),
  );
}

function fetchCatalog(w: World, query = "", key: string = w.machineKey) {
  return w.t.fetch(`/api/machine/programmes${query}`, {
    method: "GET",
    headers: { Authorization: `Bearer ${key}`, [CONTRACT_HEADER]: CONTRACT_VERSION },
  });
}

type Signed = { status: "signed"; payload: string; signature: string; server_contract_version: string };
type Payload = {
  catalogRevision: number;
  unchanged: boolean;
  programmes: Array<Record<string, unknown>>;
};

/** A catalogue of every state: two published, one retired, one draft, one of an organisation. */
async function catalogue(w: World) {
  await programme(w, "published", { profileId: "zeta_court", name: "Zeta" });
  await programme(w, "published", { profileId: "alpha_long", name: "Alpha", maxRpm: 200 });
  await programme(w, "retired", { profileId: "old_one", name: "Retired" });
  await programme(w, "draft", { profileId: "draft_one", name: "Draft" });
  await programme(w, "published", { profileId: "client_own", name: "Client" }, w.organizationId);
}

describe("ANH-138 GET /api/machine/programmes", () => {
  it("answers not_configured, and signs nothing, while the key is absent", async () => {
    const w = await seedMachineWorld(modules);
    await catalogue(w);
    const response = await fetchCatalog(w);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({
      status: "not_configured",
      server_contract_version: CONTRACT_VERSION,
    });
  });

  it("signs the published Anheart catalogue, verifiable with the public half of the key", async () => {
    const w = await seedMachineWorld(modules);
    await catalogue(w);
    const keys = await keyPair();
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, keys.privatePem);
    const response = await fetchCatalog(w);
    expect(response.status).toBe(200);
    const answer = (await response.json()) as Signed;
    expect(answer.status).toBe("signed");
    expect(answer.server_contract_version).toBe(CONTRACT_VERSION);
    expect(
      await verifyMedicalParametersSignature(answer.payload, answer.signature, keys.publicPem),
    ).toBe(true);
    // One byte changed and it no longer verifies.
    expect(
      await verifyMedicalParametersSignature(
        answer.payload.replace("zeta_court", "zeta_courT"),
        answer.signature,
        keys.publicPem,
      ),
    ).toBe(false);
    // Nor with another key.
    expect(
      await verifyMedicalParametersSignature(
        answer.payload,
        answer.signature,
        (await keyPair()).publicPem,
      ),
    ).toBe(false);
    const payload = JSON.parse(answer.payload) as Payload;
    // Two published (1 each) and one retired (1, and 1 more for its retirement).
    expect(payload.catalogRevision).toBe(4);
    expect(payload.unchanged).toBe(false);
    expect(payload.programmes.map((p) => p.profile_id)).toEqual(["alpha_long", "zeta_court"]);
    expect(payload.programmes[0]).toMatchObject({
      name: "Alpha",
      max_rpm: 200,
      total_duration_s: JOG.totalDurationS,
      channels: ["ECG"],
      medical_parameters_version: PROGRAMME_MEDICAL_VERSION,
    });
  });

  it("writes each programme with the keys of the console's profile document, and its medical version", async () => {
    const w = await seedMachineWorld(modules);
    await catalogue(w);
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, (await keyPair()).privateBase64);
    const answer = (await (await fetchCatalog(w)).json()) as Signed;
    const sent = (JSON.parse(answer.payload) as Payload).programmes[0];
    expect(Object.keys(sent).sort()).toEqual(
      [...Object.keys(shippedDefaults.profiles[0]), "medical_parameters_version"].sort(),
    );
  });

  it("leaves the programmes out when since is the revision held, signed all the same", async () => {
    const w = await seedMachineWorld(modules);
    await catalogue(w);
    const keys = await keyPair();
    vi.stubEnv(CATALOG_SIGNING_KEY_ENV, keys.privatePem);
    const same = (await (await fetchCatalog(w, "?since=4")).json()) as Signed;
    expect(JSON.parse(same.payload)).toEqual({ catalogRevision: 4, unchanged: true, programmes: [] });
    expect(
      await verifyMedicalParametersSignature(same.payload, same.signature, keys.publicPem),
    ).toBe(true);
    for (const since of ["0", "3", "5"]) {
      const other = (await (await fetchCatalog(w, `?since=${since}`)).json()) as Signed;
      expect(JSON.parse(other.payload)).toMatchObject({ catalogRevision: 4, unchanged: false });
      expect((JSON.parse(other.payload) as Payload).programmes).toHaveLength(2);
    }
  });

  it.each(["-1", "1.5", "01", "abc", "", "1e3", "9999999999999999"])(
    "refuses since=%j as invalid_request",
    async (since) => {
      const w = await seedMachineWorld(modules);
      vi.stubEnv(CATALOG_SIGNING_KEY_ENV, (await keyPair()).privatePem);
      const response = await fetchCatalog(w, `?since=${since}`);
      expect(response.status).toBe(400);
      expect(await response.json()).toMatchObject({ error: "invalid_request" });
    },
  );

  it.each(["not a key", "", "QUJD"])(
    "answers not_configured, and says so in the server log, for an unusable key %j",
    async (key) => {
      const w = await seedMachineWorld(modules);
      const logged = vi.spyOn(console, "error").mockImplementation(() => {});
      vi.stubEnv(CATALOG_SIGNING_KEY_ENV, key);
      const response = await fetchCatalog(w);
      expect(await response.json()).toEqual({
        status: "not_configured",
        server_contract_version: CONTRACT_VERSION,
      });
      // An empty variable is an absent one: nothing to say.
      expect(logged).toHaveBeenCalledTimes(key === "" ? 0 : 1);
    },
  );

  it("is opened by a machine key only", async () => {
    const w = await seedMachineWorld(modules);
    const response = await fetchCatalog(w, "", `anh1.${"ab".repeat(16)}.${"cd".repeat(32)}`);
    expect(response.status).toBe(401);
    expect(await response.json()).toMatchObject({ error: "unauthorized" });
  });

  it("counts one revision more at each publication and each retirement", () => {
    const row = (status: CatalogRow["status"], revision: number) =>
      ({ ...JOG, status, revision, medicalParametersVersion: "0.1.0" }) as CatalogRow;
    expect(catalogRevision([])).toBe(0);
    expect(catalogRevision([row("draft", 0), row("validated", 0)])).toBe(0);
    expect(catalogRevision([row("published", 1)])).toBe(1);
    expect(catalogRevision([row("retired", 1)])).toBe(2);
    expect(catalogRevision([row("published", 1), row("retired", 1), row("draft", 0)])).toBe(3);
  });
});

function sendProfiles(w: World, body: Record<string, unknown>) {
  return w.t.fetch("/api/machine/profiles", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
}

const PROFILES = { storeRev: 3, programsEnabled: true, profiles: [] };
const live = (w: World) => w.admin.query(api.training.getMachineLive, { machineId: w.machine });

describe("ANH-138 what a machine says of the catalogue (POST /api/machine/profiles)", () => {
  it("keeps the revision accepted and the programmes refused with their reason", async () => {
    const w = await seedMachineWorld(modules);
    const response = await sendProfiles(w, {
      ...PROFILES,
      catalogRevision: 4,
      accepted: ["alpha_long"],
      rejected: [{ profileId: "zeta_court", reason: "max_rpm 900 au-dessus du plafond 800" }],
    });
    expect(response.status).toBe(200);
    expect((await live(w))?.programmeCatalog).toEqual({
      revision: 4,
      rejected: [{ profileId: "zeta_court", reason: "max_rpm 900 au-dessus du plafond 800" }],
      reportedAt: expect.any(Number),
    });
  });

  it("keeps null for a machine on its fallback programmes", async () => {
    const w = await seedMachineWorld(modules);
    await sendProfiles(w, { ...PROFILES, catalogRevision: null, accepted: [], rejected: [] });
    expect((await live(w))?.programmeCatalog).toMatchObject({ revision: null, rejected: [] });
  });

  it("accepts the body of an older console, and forgets the catalogue state then", async () => {
    const w = await seedMachineWorld(modules);
    await sendProfiles(w, { ...PROFILES, catalogRevision: 2, accepted: [], rejected: [] });
    const older = await sendProfiles(w, PROFILES);
    expect(older.status).toBe(200);
    expect((await live(w))?.programmeCatalog).toBeUndefined();
  });

  const tooLong = "x".repeat(machineApi.programme_catalog.max_reason_chars + 1);
  const tooMany = Array.from({ length: machineApi.programme_catalog.max_report_entries + 1 }, (_, n) => `p${n}`);
  it.each([
    ["a revision that is not a whole number", { catalogRevision: 1.5, accepted: [], rejected: [] }],
    ["a negative revision", { catalogRevision: -1, accepted: [], rejected: [] }],
    ["a revision in words", { catalogRevision: "4", accepted: [], rejected: [] }],
    ["no accepted list", { catalogRevision: 4, rejected: [] }],
    ["no rejected list", { catalogRevision: 4, accepted: [] }],
    ["an accepted id that is not text", { catalogRevision: 4, accepted: [7], rejected: [] }],
    ["a refusal that is not an object", { catalogRevision: 4, accepted: [], rejected: ["x"] }],
    ["a refusal without its reason", { catalogRevision: 4, accepted: [], rejected: [{ profileId: "x" }] }],
    ["a reason too long", { catalogRevision: 4, accepted: [], rejected: [{ profileId: "x", reason: tooLong }] }],
    ["too many entries", { catalogRevision: 4, accepted: tooMany, rejected: [] }],
  ])("refuses %s, and changes nothing", async (_, report) => {
    const w = await seedMachineWorld(modules);
    const response = await sendProfiles(w, { ...PROFILES, ...report });
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ error: "invalid_request" });
    expect((await live(w))?.programmeCatalog).toBeUndefined();
    const kept = await w.t.run((ctx) =>
      ctx.db
        .query("machine_profiles")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .collect(),
    );
    expect(kept).toHaveLength(1);
  });
});
