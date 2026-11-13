/// <reference types="vite/client" />
/**
 * ANH-145: the incident register.
 *
 * - EX-2: what opens an incident, for each kind, and what opens none; one
 *   incident per `(sessionId, rule, t)`, however many times the machine sends
 *   it;
 * - EX-3: the severity rule of `convex/lib/incidents.ts`;
 * - EX-4: a high incident open or analysed refuses programmed sessions (the
 *   launch, the poll's offer, the arming, the sweep), never a bench session,
 *   and its closing by the Anheart admin lifts it; the poll announces the
 *   count;
 * - EX-5: the alert to the Anheart admins, the sheet's events around the
 *   instant, the analysis and the closing. The rights, role by role, are in
 *   `authorization.matrix.ts`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import shared from "../contracts/machine-api.json";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import {
  levelRank,
  PREFLIGHT_REFUSAL,
  severityOf,
  UNKNOWN_LEVEL,
} from "./lib/incidents";
import {
  INCIDENT_EVENT_WINDOW_MS,
  INCIDENT_TEXT_MAX,
} from "./incidents";
import {
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  seedMachineWorld,
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

type Occupancy = "auto" | "occupied" | "bench";

/** An active session of `machine`: a programme, a manual one occupied, or the bench. */
function session(w: World, occupancy: Occupancy = "auto") {
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
      notes: occupancy === "occupied" ? "Occupancy: occupied" : "Occupancy: bench",
    }),
  );
}

let seq = 0;
function sendEvents(
  w: World,
  sessionId: Id<"sessions">,
  events: { t?: number; kind: string; detail: string; seq?: number }[],
) {
  return w.t.mutation(internal.training.storeEvents, {
    machineId: w.machine,
    sessionId,
    events: events.map((event) => ({
      seq: event.seq ?? ++seq,
      t: event.t ?? NOW,
      kind: event.kind,
      detail: event.detail,
      actor: "system",
    })),
  });
}

function end(w: World, sessionId: Id<"sessions">, reason: string, failed = true) {
  return w.t.mutation(internal.training.endTrainingSession, {
    machineId: w.machine,
    sessionId,
    failed,
    reason,
    endedAt: NOW + 5_000,
  });
}

const incidents = (w: Pick<World, "t">) =>
  w.t.run((ctx) => ctx.db.query("incidents").collect());

const summary = (rows: Doc<"incidents">[]) =>
  rows.map(({ kind, rule, severity, occupied, status }) => ({
    kind,
    rule,
    severity,
    occupied,
    status,
  }));

async function codeOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

function launch(w: World) {
  return as(w.t, "manager").mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    userId: w.patient,
    profileId: w.profileId,
  });
}

const close = (w: World, incidentId: Id<"incidents">) =>
  as(w.t, "admin").mutation(api.incidents.close, {
    incidentId,
    analysis: "capteur ECG decolle",
    action: "electrodes changees, consigne rappelee",
  });

// ---------------------------------------------------------------------------
// EX-3: the severity
// ---------------------------------------------------------------------------

describe("ANH-145 EX-3 the severity of an incident", () => {
  it.each([
    ["safety_verdict", true, "QUICK_STOP", "high"],
    ["safety_verdict", true, "RAMP_DOWN", "high"],
    ["safety_verdict", false, "QUICK_STOP", "medium"],
    ["safety_verdict", false, "GO_SILENT", "high"],
    ["safety_verdict", true, "GO_SILENT", "high"],
    ["safety_verdict", false, UNKNOWN_LEVEL, "high"],
    ["drive_fault", true, "RAMP_DOWN", "high"],
    ["drive_fault", false, "RAMP_DOWN", "medium"],
    ["emergency_stop", true, "QUICK_STOP", "high"],
    ["emergency_stop", false, "QUICK_STOP", "low"],
    ["tick_exception", false, null, "high"],
    ["tick_exception", true, null, "high"],
    ["preflight_repeated_failure", true, null, "low"],
    ["record_rejected", true, null, "low"],
    ["manual", false, null, "low"],
    ["manual", true, "REDUCE", "low"],
  ] as const)("%s, occupied %s, at %s: %s", (kind, occupied, level, expected) => {
    expect(severityOf(kind, occupied, level)).toBe(expected);
  });

  it("ranks the levels as the Pi orders them, an unknown one above all", () => {
    expect(levelRank(null)).toBe(-1);
    expect(levelRank("FREEZE")).toBeLessThan(levelRank("REDUCE"));
    expect(levelRank("REDUCE")).toBeLessThan(levelRank("RAMP_DOWN"));
    expect(levelRank("RAMP_DOWN")).toBeLessThan(levelRank("QUICK_STOP"));
    expect(levelRank("QUICK_STOP")).toBeLessThan(levelRank("GO_SILENT"));
    expect(levelRank("GO_SILENT")).toBeLessThan(levelRank(UNKNOWN_LEVEL));
  });
});

// ---------------------------------------------------------------------------
// EX-2: what opens an incident
// ---------------------------------------------------------------------------

describe("ANH-145 EX-2 an event opens an incident", () => {
  it("opens one high incident for a QUICK_STOP verdict with a person on board, the session going on", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    await sendEvents(w, sessionId, [
      { kind: "verdict", detail: "hr_critical (QUICK_STOP): 190 bpm" },
    ]);
    expect(summary(await incidents(w))).toEqual([
      {
        kind: "safety_verdict",
        rule: "hr_critical",
        severity: "high",
        occupied: true,
        status: "open",
      },
    ]);
    const [row] = await incidents(w);
    expect(row).toMatchObject({
      organizationId: w.orgA,
      machineId: w.machine,
      sessionId,
      t: NOW,
      openedAt: NOW,
    });
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
      "active",
    );
  });

  it("opens none for a verdict that regulates, ramps down, or is lifted", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    await sendEvents(w, sessionId, [
      { kind: "verdict", detail: "hr_hard_max (FREEZE): 175 bpm" },
      { kind: "verdict", detail: "hr_hard_max (REDUCE): 176 bpm" },
      { kind: "verdict", detail: "hr_drop (RAMP_DOWN): fall" },
      { kind: "verdict", detail: "cleared" },
      { kind: "phase", detail: "hold" },
      { kind: "drive_fault", detail: "cleared" },
    ]);
    expect(await incidents(w)).toEqual([]);
  });

  it("opens one for a GO_SILENT verdict at the bench, high, and a QUICK_STOP one there, medium", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w, "bench");
    await sendEvents(w, sessionId, [
      { t: NOW, kind: "verdict", detail: "comms_lost (GO_SILENT): no answer" },
      { t: NOW + 1, kind: "verdict", detail: "overspeed (QUICK_STOP): 320 rpm" },
    ]);
    expect(summary(await incidents(w))).toEqual([
      expect.objectContaining({ rule: "comms_lost", severity: "high", occupied: false }),
      expect.objectContaining({ rule: "overspeed", severity: "medium", occupied: false }),
    ]);
  });

  it("opens one, high, for a verdict whose action this server cannot read", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w, "bench");
    await sendEvents(w, sessionId, [
      { kind: "verdict", detail: "new_rule (FULL_BRAKE): later" },
      { t: NOW + 1, kind: "verdict", detail: "not the Pi's form" },
    ]);
    expect(summary(await incidents(w))).toEqual([
      expect.objectContaining({ rule: "new_rule", severity: "high" }),
      expect.objectContaining({ rule: "unknown", severity: "high" }),
    ]);
  });

  it("opens one for a drive fault, named by its mnemonic: high with a person on board, medium at the bench", async () => {
    const w = await seedLaunchWorld(modules);
    const occupied = await session(w, "occupied");
    const bench = await session(w, "bench");
    await sendEvents(w, occupied, [
      { kind: "drive_fault", detail: "OHF (LFT 17): drive overheat" },
    ]);
    await sendEvents(w, bench, [
      { kind: "drive_fault", detail: "an unreadable fault" },
    ]);
    expect(summary(await incidents(w))).toEqual([
      expect.objectContaining({ kind: "drive_fault", rule: "OHF", severity: "high" }),
      expect.objectContaining({
        kind: "drive_fault",
        rule: "drive_fault",
        severity: "medium",
      }),
    ]);
  });

  it("opens one incident for an event sent twice, and for the same verdict at the same instant under another rank", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    const event = { seq: 7, kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" };
    expect(await sendEvents(w, sessionId, [event])).toMatchObject({ stored: 1 });
    expect(await sendEvents(w, sessionId, [event])).toMatchObject({ duplicates: 1 });
    await sendEvents(w, sessionId, [{ ...event, seq: 8 }]);
    expect(await incidents(w)).toHaveLength(1);
    // Another instant is another verdict.
    await sendEvents(w, sessionId, [{ ...event, seq: 9, t: NOW + 1000 }]);
    expect(await incidents(w)).toHaveLength(2);
  });
});

describe("ANH-145 EX-2 an end opens an incident", () => {
  it.each([
    ["emergency_stop", "auto", "high"],
    ["emergency_stop: operateur", "bench", "low"],
    ["tick_exception", "bench", "high"],
  ] as const)("%s (%s): %s", async (reason, occupancy, severity) => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w, occupancy);
    await end(w, sessionId, reason);
    await end(w, sessionId, reason);
    const kind = reason.split(":")[0];
    expect(await incidents(w)).toEqual([
      expect.objectContaining({ kind, rule: kind, severity, t: NOW + 5_000 }),
    ]);
  });

  it.each(["programme_complete", "operator_stop: fin", "shutdown"])(
    "opens none for an end %s",
    async (reason) => {
      const w = await seedLaunchWorld(modules);
      await end(w, await session(w), reason, reason === "shutdown");
      expect(await incidents(w)).toEqual([]);
    },
  );

  it("takes the verdict that already opened one, and opens no second for the end", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    await sendEvents(w, sessionId, [
      { t: NOW + 1000, kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" },
    ]);
    await end(w, sessionId, "safety_verdict");
    expect(summary(await incidents(w))).toEqual([
      expect.objectContaining({ kind: "safety_verdict", rule: "hr_critical" }),
    ]);
  });

  it("opens one for an end on a ramp-down verdict, with its rule and instant, high with a person on board", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    await sendEvents(w, sessionId, [
      { t: NOW + 1000, kind: "verdict", detail: "hr_drop (RAMP_DOWN): fall" },
      { t: NOW + 2000, kind: "verdict", detail: "cleared" },
      { t: NOW + 3000, kind: "verdict", detail: "hr_hard_max (FREEZE): 175" },
    ]);
    await end(w, sessionId, "safety_verdict");
    expect(await incidents(w)).toEqual([
      expect.objectContaining({
        kind: "safety_verdict",
        rule: "hr_drop",
        t: NOW + 1000,
        severity: "high",
      }),
    ]);
  });

  it("opens one named after the end when no verdict was received, medium at the bench", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w, "bench");
    await end(w, sessionId, "safety_verdict");
    expect(await incidents(w)).toEqual([
      expect.objectContaining({
        kind: "safety_verdict",
        rule: "safety_verdict",
        t: NOW + 5_000,
        severity: "medium",
      }),
    ]);
  });

  it("opens one at the third pre-flight refusal in a row on a machine, and no more at the fourth", async () => {
    const w = await seedLaunchWorld(modules);
    const refuse = async (offset: number, reason = `${PREFLIGHT_REFUSAL} batterie BITalino 20 % < 30 %`) => {
      vi.setSystemTime(NOW + offset);
      const sessionId = await w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId: w.patient,
          startedById: w.manager,
          status: "pending",
          startedAt: NOW + offset,
          channels: ["ECG"],
          kind: "auto",
          origin: "remote",
        }),
      );
      await end(w, sessionId, reason);
      return sessionId;
    };
    await refuse(0);
    await refuse(1000, "refusee par la machine : une seance est deja en cours");
    await refuse(2000);
    await refuse(3000);
    expect(await incidents(w)).toEqual([]);
    const third = await refuse(4000);
    await refuse(5000);
    expect(await incidents(w)).toEqual([
      expect.objectContaining({
        kind: "preflight_repeated_failure",
        rule: "preflight",
        sessionId: third,
        severity: "low",
      }),
    ]);
  });
});

describe("ANH-145 EX-2 a rejected record opens an incident", () => {
  it("opens one when the record becomes rejected, with the rejected file, once", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    const files = await w.t.run(async (ctx) => {
      const ids: Id<"session_record_files">[] = [];
      for (const path of ["manifest.json", "ticks.csv"]) {
        const storageId = await ctx.storage.store(new Blob([path]));
        ids.push(
          await ctx.db.insert("session_record_files", {
            sessionId,
            machineId: w.machine,
            organizationId: w.orgA,
            path,
            storageId,
            sha256: "0".repeat(64),
            size: path.length,
            uploadedAt: NOW,
            verified: false,
          }),
        );
      }
      return ids;
    });
    for (const fileId of files) {
      const file = await w.t.run((ctx) => ctx.db.get(fileId));
      await w.t.mutation(internal.recordStorage.finishVerification, {
        fileId,
        storageId: file!.storageId,
        rejection: "sha256_mismatch",
      });
    }
    expect(await incidents(w)).toEqual([
      expect.objectContaining({
        kind: "record_rejected",
        rule: "sha256_mismatch",
        recordId: files[0],
        sessionId,
        severity: "low",
      }),
    ]);
  });
});

// ---------------------------------------------------------------------------
// EX-5: the alert to the Anheart admins
// ---------------------------------------------------------------------------

describe("ANH-145 EX-5 a high incident alerts the Anheart admins", () => {
  it("writes one alert to the Anheart admin, in the Anheart organisation, and none to the machine's staff", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    await sendEvents(w, sessionId, [
      { kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" },
      { t: NOW + 1, kind: "verdict", detail: "hr_critical (QUICK_STOP): 191" },
    ]);
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    const alerts = await w.t.run((ctx) =>
      ctx.db
        .query("notifications")
        .filter((q) => q.eq(q.field("type"), "incident_high"))
        .collect(),
    );
    expect(alerts).toEqual([
      expect.objectContaining({
        userId: w.admin,
        organizationId: w.anheartOrg,
        machineId: w.machine,
        sessionId,
        count: 2,
      }),
    ]);
    const listed = await as(w.t, "admin").query(api.notifications.list, {});
    expect(listed.map((row) => [row.type, row.sessionId])).toEqual([
      ["incident_high", sessionId],
    ]);
  });

  it("alerts nobody of an incident that is not high", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const w = await seedLaunchWorld(modules);
    await sendEvents(w, await session(w, "bench"), [
      { kind: "drive_fault", detail: "OHF (LFT 17): overheat" },
    ]);
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    const alerts = await w.t.run((ctx) =>
      ctx.db
        .query("notifications")
        .filter((q) => q.eq(q.field("type"), "incident_high"))
        .collect(),
    );
    expect(alerts).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// EX-4: what a high incident closes
// ---------------------------------------------------------------------------

async function highIncident(w: World): Promise<Id<"incidents">> {
  const sessionId = await session(w);
  await sendEvents(w, sessionId, [
    { kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" },
  ]);
  await end(w, sessionId, "safety_verdict");
  const [row] = await incidents(w);
  return row._id;
}

describe("ANH-145 EX-4 a high incident closes programmed sessions until the Anheart admin closes it", () => {
  it("refuses a launch while it is open, then analysed, and accepts it once closed", async () => {
    const w = await seedLaunchWorld(modules);
    const incidentId = await highIncident(w);
    expect(await codeOf(launch(w))).toBe("incident_open");
    await as(w.t, "manager").mutation(api.incidents.analyze, {
      incidentId,
      analysis: "  electrodes decollees  ",
      action: "electrodes changees",
    });
    expect(await w.t.run((ctx) => ctx.db.get(incidentId))).toMatchObject({
      status: "analyzed",
      analysis: "electrodes decollees",
      analyzedBy: w.manager,
    });
    expect(await codeOf(launch(w))).toBe("incident_open");
    await close(w, incidentId);
    expect(await w.t.run((ctx) => ctx.db.get(incidentId))).toMatchObject({
      status: "closed",
      analyzedBy: w.manager,
      closedBy: w.admin,
      closedAt: NOW,
    });
    expect(await codeOf(launch(w))).toBeNull();
  });

  it("closes nothing for an incident that is not high, nor on another machine", async () => {
    const w = await seedLaunchWorld(modules);
    const bench = await session(w, "bench");
    await sendEvents(w, bench, [
      { kind: "drive_fault", detail: "OHF (LFT 17): overheat" },
    ]);
    await end(w, bench, "operator_stop", false);
    expect(summary(await incidents(w))).toEqual([
      expect.objectContaining({ severity: "medium" }),
    ]);
    await w.t.run((ctx) =>
      ctx.db.insert("incidents", {
        organizationId: w.orgA,
        machineId: w.otherMachine,
        kind: "manual",
        rule: "manual",
        t: NOW,
        severity: "high",
        occupied: false,
        status: "open",
        openedAt: NOW,
      }),
    );
    expect(await codeOf(launch(w))).toBeNull();
  });

  it("leaves a bench launch possible", async () => {
    const w = await seedLaunchWorld(modules);
    await highIncident(w);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { contractVersion: "1.10" }));
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchBenchSession,
      { machineId: w.machine, ballastKg: 80 },
    );
    expect(
      await w.t.query(internal.training.getPendingTrainingSession, {
        machineId: w.machine,
        contract: "1.10",
      }),
    ).toMatchObject({ sessionId, mode: "bench" });
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({ armed: true });
  });

  it("withdraws a launch made before the incident opened: not offered, then refused and failed at arming", async () => {
    const w = await seedLaunchWorld(modules);
    const launched = await launch(w);
    const offered = () =>
      w.t.query(internal.training.getPendingTrainingSession, {
        machineId: w.machine,
        contract: CONTRACT_VERSION,
      });
    expect((await offered())?.sessionId).toBe(launched);
    await w.t.run((ctx) =>
      ctx.db.insert("incidents", {
        organizationId: w.orgA,
        machineId: w.machine,
        kind: "record_rejected",
        rule: "sha256_mismatch",
        t: NOW,
        severity: "high",
        occupied: true,
        status: "analyzed",
        openedAt: NOW,
      }),
    );
    expect(await offered()).toBeNull();
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId: launched,
      }),
    ).toMatchObject({ armed: false, code: "incident_open" });
    expect(await w.t.run((ctx) => ctx.db.get(launched))).toMatchObject({
      status: "failed",
      endCode: "incident_open",
    });
  });

  it("is a refusal of the machine contract, and of the dashboard's table", () => {
    expect(shared.arming_refusals.codes).toContain("incident_open");
    expect(shared.error_codes.incident_open.statuses).toEqual([409]);
    expect(shared.incidents).toMatchObject({
      since: "1.10",
      route: "GET /api/machine/training/poll",
      field: "incidents_open_high",
    });
  });
});

describe("ANH-145 EX-4 the poll announces the machine's high incidents", () => {
  it("answers incidents_open_high: 0, then the count of those open or analysed, and 0 again once closed", async () => {
    const w = await seedMachineWorld(modules);
    const poll = async () => {
      const response = await w.t.fetch("/api/machine/training/poll", {
        method: "GET",
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          [CONTRACT_HEADER]: CONTRACT_VERSION,
        },
      });
      expect(response.status).toBe(200);
      return ((await response.json()) as Record<string, unknown>)
        .incidents_open_high;
    };
    expect(await poll()).toBe(0);
    const insert = (
      machineId: Id<"machines">,
      severity: "high" | "medium",
      status: "open" | "analyzed" | "closed",
    ) =>
      w.t.run((ctx) =>
        ctx.db.insert("incidents", {
          organizationId: w.organizationId,
          machineId,
          kind: "manual",
          rule: "manual",
          t: NOW,
          severity,
          occupied: false,
          status,
          openedAt: NOW,
        }),
      );
    const first = await insert(w.machine, "high", "open");
    await insert(w.machine, "high", "analyzed");
    await insert(w.machine, "high", "closed");
    await insert(w.machine, "medium", "open");
    await insert(w.otherMachine, "high", "open");
    vi.setSystemTime(NOW + 2_000);
    expect(await poll()).toBe(2);
    await w.admin.mutation(api.incidents.close, {
      incidentId: first,
      analysis: "cause",
      action: "action",
    });
    vi.setSystemTime(NOW + 4_000);
    expect(await poll()).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// EX-5: the sheet, the analysis and the closing
// ---------------------------------------------------------------------------

describe("ANH-145 EX-5 an incident's sheet, its analysis and its closing", () => {
  it("shows the session's events within 60 s of the instant, and who analysed and closed it", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await session(w);
    const at = NOW + 100_000;
    await sendEvents(w, sessionId, [
      { t: at - INCIDENT_EVENT_WINDOW_MS - 1, kind: "phase", detail: "warmup" },
      { t: at - INCIDENT_EVENT_WINDOW_MS, kind: "phase", detail: "hold" },
      { t: at, kind: "verdict", detail: "hr_critical (QUICK_STOP): 190" },
      { t: at + INCIDENT_EVENT_WINDOW_MS, kind: "end", detail: "safety_verdict" },
      { t: at + INCIDENT_EVENT_WINDOW_MS + 1, kind: "phase", detail: "late" },
    ]);
    const [row] = await incidents(w);
    await close(w, row._id);
    const sheet = await as(w.t, "manager").query(api.incidents.get, {
      incidentId: row._id,
    });
    expect(sheet?.events.map((event) => event.detail)).toEqual([
      "hold",
      "hr_critical (QUICK_STOP): 190",
      "safety_verdict",
    ]);
    expect(sheet).toMatchObject({
      incident: { _id: row._id, machineName: "Synthetic machine", status: "closed" },
      analyzedByName: "admin Synthetic",
      closedByName: "admin Synthetic",
      session: { _id: sessionId, status: "active" },
      canClose: false,
    });
  });

  it("refuses an analysis or a closing without its cause and its action, or too long, and a closed incident", async () => {
    const w = await seedLaunchWorld(modules);
    const incidentId = await highIncident(w);
    const analyze = (analysis: string, action: string) =>
      codeOf(
        as(w.t, "manager").mutation(api.incidents.analyze, {
          incidentId,
          analysis,
          action,
        }),
      );
    expect(await analyze("  ", "action")).toBe("incident_text_required");
    expect(await analyze("cause", "")).toBe("incident_text_required");
    expect(await analyze("x".repeat(INCIDENT_TEXT_MAX + 1), "action")).toBe(
      "incident_text_required",
    );
    expect(
      await codeOf(
        as(w.t, "admin").mutation(api.incidents.close, {
          incidentId,
          analysis: "cause",
          action: " ",
        }),
      ),
    ).toBe("incident_text_required");
    expect((await w.t.run((ctx) => ctx.db.get(incidentId)))?.status).toBe("open");
    expect(await analyze("x".repeat(INCIDENT_TEXT_MAX), "action")).toBeNull();
    await close(w, incidentId);
    expect(await analyze("cause", "action")).toBe("incident_closed");
    expect(await codeOf(close(w, incidentId))).toBe("incident_closed");
  });

  it("refuses to close an incident that does not exist", async () => {
    const w = await seedLaunchWorld(modules);
    const incidentId = await highIncident(w);
    await w.t.run((ctx) => ctx.db.delete(incidentId));
    expect(await codeOf(close(w, incidentId))).toBe("incident_not_found");
  });
});

// ---------------------------------------------------------------------------
// Acceptance, as the ticket writes it, through the routes a console calls
// ---------------------------------------------------------------------------

describe("ANH-145 acceptance: hr_critical with a person on board", () => {
  it("opens a high incident, refuses the next launch « incident ouvert », and allows it again once the admin closes it", async () => {
    const w = await seedLaunchWorld(modules);
    const sessionId = await launch(w);
    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({ armed: true });
    const verdict = {
      seq: 3,
      kind: "verdict",
      detail: "hr_critical (QUICK_STOP): 190 bpm >= 185 bpm",
    };
    // Two sends of the same event: one incident.
    await sendEvents(w, sessionId, [verdict]);
    await sendEvents(w, sessionId, [verdict]);
    await end(w, sessionId, "safety_verdict");
    const [incident, ...others] = await incidents(w);
    expect(others).toEqual([]);
    expect(incident).toMatchObject({
      kind: "safety_verdict",
      rule: "hr_critical",
      severity: "high",
      occupied: true,
      status: "open",
    });
    expect(await codeOf(launch(w))).toBe("incident_open");
    await as(w.t, "admin").mutation(api.incidents.analyze, {
      incidentId: incident._id,
      analysis: "FC critique reelle",
      action: "seance arretee, medecin consulte",
    });
    expect(await codeOf(launch(w))).toBe("incident_open");
    await close(w, incident._id);
    expect(await codeOf(launch(w))).toBeNull();
  });
});
