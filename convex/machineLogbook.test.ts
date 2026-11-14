/**
 * ANH-149: the machine logbook. Who reads and writes it (EX-3), what the
 * server writes on it (EX-2), and the drive fault rule (EX-4).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import driveFaults from "../contracts/drive-faults.json";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_VERSION } from "./lib/contract";
import { manualResetOnly } from "./lib/logbook";
import { LOGBOOK_LIST_LIMIT } from "./machineLogbook";
import { registryRelease, registryUpload } from "./machineRegistry.fixtures";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  type World,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

async function codeOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

function session(w: World, occupancy: "auto" | "bench" = "auto") {
  return w.t.run((ctx) =>
    ctx.db.insert("sessions", {
      organizationId: w.orgA,
      machineId: w.machine,
      userId: occupancy === "bench" ? undefined : w.patient,
      startedById: w.manager,
      status: "active",
      startedAt: NOW,
      channels: ["ECG"],
      kind: occupancy === "auto" ? "auto" : "manual",
      origin: "local",
      notes: occupancy === "bench" ? "Occupancy: bench" : undefined,
    }),
  );
}

let seq = 0;
async function driveFault(w: World, detail: string, occupancy: "auto" | "bench" = "auto") {
  const sessionId = await session(w, occupancy);
  await w.t.mutation(internal.training.storeEvents, {
    machineId: w.machine,
    sessionId,
    events: [{ seq: ++seq, t: NOW + seq, kind: "drive_fault", detail, actor: "system" }],
  });
  const incident = await w.t.run((ctx) =>
    ctx.db
      .query("incidents")
      .withIndex("by_session", (q) => q.eq("sessionId", sessionId))
      .first(),
  );
  if (incident === null) throw new Error("no incident opened");
  await w.t.run((ctx) => ctx.db.patch(sessionId, { status: "failed", endedAt: NOW }));
  return incident._id;
}

const launch = (w: World) =>
  as(w.t, "manager").mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    userId: w.patient,
    profileId: w.profileId,
  });

const close = (w: World, incidentId: Id<"incidents">) =>
  as(w.t, "admin").mutation(api.incidents.close, {
    incidentId,
    analysis: "court-circuit moteur",
    action: "bobinage controle",
  });

const intervention = (w: World, incidentId?: Id<"incidents">) =>
  as(w.t, "admin").mutation(api.machineLogbook.addEntry, {
    machineId: w.machine,
    kind: "intervention",
    summary: "variateur inspecte, cable moteur remplace",
    incidentId,
  });

const read = (w: World, actor: Parameters<typeof as>[1] = "admin") =>
  as(w.t, actor).query(api.machineLogbook.list, { machineId: w.machine });

async function kinds(w: World) {
  const book = await read(w);
  return book === null ? null : book.entries.map((entry) => [entry.kind, entry.summary]);
}

describe("EX-3 who reads and writes the logbook", () => {
  it("is read by whoever reaches the machine, and by nobody else", async () => {
    const w = await seedLaunchWorld(modules);
    await as(w.t, "orgAdmin").mutation(api.machineLogbook.addEntry, {
      machineId: w.machine,
      kind: "note",
      summary: "machine deplacee en salle 2",
    });
    for (const actor of ["admin", "orgAdmin", "manager"] as const) {
      expect((await read(w, actor))?.entries).toHaveLength(1);
    }
    expect(await read(w, "orgBAdmin")).toBeNull();
    expect(await read(w, "otherManager")).toBeNull();
    expect(await codeOf(read(w, "patient"))).toBe("not_authorized");
    expect((await read(w, "admin"))?.writable).toEqual([
      "intervention",
      "calibration",
      "part_replaced",
      "note",
    ]);
    expect((await read(w, "orgAdmin"))?.writable).toEqual(["note"]);
    expect((await read(w, "manager"))?.writable).toEqual([]);
  });

  it("is written by hand by an Anheart admin, and by an org_admin of its organisation for notes only", async () => {
    const w = await seedLaunchWorld(modules);
    const note = { machineId: w.machine, kind: "note" as const, summary: "rien a signaler" };
    expect(
      await codeOf(
        as(w.t, "orgAdmin").mutation(api.machineLogbook.addEntry, {
          ...note,
          kind: "intervention",
        }),
      ),
    ).toBe("logbook_kind_not_allowed");
    expect(await codeOf(as(w.t, "orgBAdmin").mutation(api.machineLogbook.addEntry, note))).toBe(
      "machine_not_found",
    );
    expect(await codeOf(as(w.t, "manager").mutation(api.machineLogbook.addEntry, note))).toBe(
      "not_authorized",
    );
    for (const kind of ["software_update", "incident", "config_change"] as const) {
      expect(
        await codeOf(as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...note, kind })),
      ).toBe("logbook_kind_not_allowed");
    }
    for (const summary of ["ab", "x".repeat(201)]) {
      expect(
        await codeOf(as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...note, summary })),
      ).toBe("invalid_logbook_entry");
    }
    expect(
      await codeOf(
        as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...note, at: NOW + 1 }),
      ),
    ).toBe("invalid_logbook_entry");
    const incidentId = await driveFault(w, "OHF (LFT 17): drive overheat");
    expect(
      await codeOf(as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...note, incidentId })),
    ).toBe("invalid_logbook_entry");
    await as(w.t, "admin").mutation(api.machineLogbook.addEntry, {
      ...note,
      kind: "calibration",
      details: "capteur de vitesse recale sur le tachymetre",
      at: NOW - 60_000,
    });
    const audits = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(audits.filter((row) => row.action === "logbook.entry_added")).toHaveLength(1);
    const entry = (await read(w))?.entries.find((row) => row.kind === "calibration");
    expect(entry).toMatchObject({ at: NOW - 60_000, by: w.admin, byName: expect.any(String) });
  });

  it("attaches one fresh PDF or image, once", async () => {
    const w = await seedLaunchWorld(modules);
    const url = await as(w.t, "orgAdmin").mutation(api.machineLogbook.generateUploadUrl, {});
    expect(typeof url).toBe("string");
    expect(
      await codeOf(as(w.t, "manager").mutation(api.machineLogbook.generateUploadUrl, {})),
    ).toBe("not_authorized");
    const photo = await registryUpload(w, "image/png");
    const entry = {
      machineId: w.machine,
      kind: "part_replaced" as const,
      summary: "roulement du bras remplace",
      storageId: photo,
    };
    await as(w.t, "admin").mutation(api.machineLogbook.addEntry, entry);
    expect(await codeOf(as(w.t, "admin").mutation(api.machineLogbook.addEntry, entry))).toBe(
      "invalid_logbook_file",
    );
    for (const storageId of [
      await registryUpload(w, "text/plain"),
      await registryUpload(w, "application/pdf", 10 * 1024 * 1024 + 1),
    ]) {
      expect(
        await codeOf(as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...entry, storageId })),
      ).toBe("invalid_logbook_file");
    }
    const stale = await registryUpload(w);
    vi.setSystemTime(NOW + 3_600_001);
    expect(
      await codeOf(
        as(w.t, "admin").mutation(api.machineLogbook.addEntry, { ...entry, storageId: stale }),
      ),
    ).toBe("invalid_logbook_file");
    const [shown] = (await read(w))?.entries ?? [];
    expect(shown?.attachmentUrl).toEqual(expect.any(String));
  });

  it("shows the most recent entries first, at most its limit", async () => {
    const w = await seedLaunchWorld(modules);
    await w.t.run(async (ctx) => {
      for (let i = 0; i <= LOGBOOK_LIST_LIMIT; i++) {
        await ctx.db.insert("machine_logbook", {
          machineId: w.machine,
          organizationId: w.orgA,
          at: NOW - i,
          kind: "note",
          summary: `note ${i}`,
          refs: {},
        });
      }
    });
    const entries = (await read(w))?.entries ?? [];
    expect(entries).toHaveLength(LOGBOOK_LIST_LIMIT);
    expect(entries[0]?.summary).toBe("note 0");
    expect(entries[0]?.byName).toBeNull();
  });
});

describe("EX-2 what the server writes", () => {
  it("writes an incident opened and closed, a review, a drive sheet and a software update", async () => {
    const w = await seedLaunchWorld(modules);
    const incidentId = await driveFault(w, "OHF (LFT 17): drive overheat");
    await close(w, incidentId);
    await registryRelease(w);
    await as(w.t, "admin").mutation(api.machineRegistry.addValidationReview, {
      machineId: w.machine,
      level: "bench",
      reportStorageId: await registryUpload(w),
      softwareVersion: "pi-0.1.0",
      medicalParametersVersion: null,
    });
    await as(w.t, "admin").mutation(api.machineRegistry.lowerValidationLevel, {
      machineId: w.machine,
      level: "none",
      reason: "capteur a verifier",
    });
    await as(w.t, "admin").mutation(api.machineRegistry.attachDriveParameterSheet, {
      machineId: w.machine,
      storageId: await registryUpload(w),
      version: "v3",
      signedBy: "bureau d'etudes",
      at: NOW - 1000,
    });
    const beat = (softwareVersion: string | undefined) =>
      w.t.mutation(internal.machines.recordHeartbeat, {
        machineId: w.machine,
        contractVersion: CONTRACT_VERSION,
        softwareVersion,
        medicalParametersVersion: null,
      });
    await beat("pi-0.1.0");
    await beat("pi-0.1.0");
    await beat("pi-0.2.0");
    await beat(undefined);
    await beat("pi-0.2.0");
    expect((await kinds(w))?.reverse()).toEqual([
      ["incident", "incident drive_fault OHF ouvert (high)"],
      ["incident", "incident drive_fault OHF clos"],
      ["validation_review", "revue de validation : niveau bench"],
      ["validation_review", "niveau de validation abaissé de bench à none"],
      ["drive_parameter_change", "fiche variateur v3 jointe"],
      ["software_update", "logiciel pi-0.1.0 -> pi-0.2.0"],
    ]);
    const book = await read(w);
    const opened = book?.entries.find((entry) => entry.summary.includes("ouvert"));
    expect(opened?.refs.incidentId).toBe(incidentId);
    expect(opened?.by).toBeUndefined();
  });

  it("writes an incident opened again when it becomes high once closed", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w, "bench");
    await w.t.mutation(internal.training.endTrainingSession, {
      machineId: w.machine,
      sessionId,
      reason: "emergency_stop",
      failed: true,
    });
    const placeholder = await w.t.run((ctx) => ctx.db.query("incidents").first());
    if (placeholder === null) throw new Error("no incident");
    await close(w, placeholder._id);
    await w.t.mutation(internal.training.storeEvents, {
      machineId: w.machine,
      sessionId,
      events: [
        {
          seq: ++seq,
          t: NOW + 1000,
          kind: "verdict",
          detail: "operator_estop (GO_SILENT): ecran",
          actor: "system",
        },
      ],
    });
    expect((await kinds(w))?.map(([, summary]) => summary).reverse()).toEqual([
      "incident emergency_stop emergency_stop ouvert (low)",
      "incident emergency_stop emergency_stop clos",
      "incident emergency_stop operator_estop rouvert (high)",
    ]);
  });
});

describe("EX-4 a drive fault the console cannot reset closes programmed sessions", () => {
  it("reads the console's table: each code reset at the console or not, an unknown one not", () => {
    expect(manualResetOnly("OCF")).toBe(true);
    expect(manualResetOnly("OHF")).toBe(false);
    expect(manualResetOnly("drive_fault")).toBe(true);
    expect(manualResetOnly("constructor")).toBe(true);
    const policies = new Set(Object.values(driveFaults.faults));
    expect([...policies].sort()).toEqual(driveFaults.policies);
  });

  it("refuses an automatic launch until the incident is closed and an intervention names it", async () => {
    const w = await seedLaunchWorld(modules);
    const incidentId = await driveFault(w, "OCF (LFT 23): overcurrent", "bench");
    expect((await read(w))?.interventionsDue).toEqual([incidentId]);
    expect(await codeOf(launch(w))).toBe("drive_intervention_required");
    await close(w, incidentId);
    expect(await codeOf(launch(w))).toBe("drive_intervention_required");
    await intervention(w);
    expect(await codeOf(launch(w))).toBe("drive_intervention_required");
    await intervention(w, incidentId);
    expect((await read(w))?.interventionsDue).toEqual([]);
    expect(await codeOf(launch(w))).toBeNull();
  });

  it("asks for an intervention before the closure too, and for a fault whose code is unreadable", async () => {
    const w = await seedLaunchWorld(modules);
    const unreadable = await driveFault(w, "an unreadable fault", "bench");
    await intervention(w, unreadable);
    expect(await codeOf(launch(w))).toBe("drive_intervention_required");
    await close(w, unreadable);
    expect(await codeOf(launch(w))).toBeNull();
  });

  it("leaves a fault the console resets, and another machine's intervention, out of it", async () => {
    const w = await seedLaunchWorld(modules);
    const resettable = await driveFault(w, "OHF (LFT 17): drive overheat", "bench");
    expect((await read(w))?.interventionsDue).toEqual([]);
    expect(await codeOf(launch(w))).toBeNull();
    const foreign = await w.t.run((ctx) =>
      ctx.db.insert("incidents", {
        organizationId: w.orgA,
        machineId: w.otherMachine,
        kind: "drive_fault",
        rule: "OCF",
        t: NOW,
        severity: "medium",
        occupied: false,
        status: "closed",
        openedAt: NOW,
      }),
    );
    expect(await codeOf(intervention(w, foreign))).toBe("logbook_incident_mismatch");
    expect(resettable).toBeDefined();
  });
});
