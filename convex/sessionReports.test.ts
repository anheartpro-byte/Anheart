/**
 * ANH-89: the summary of a finished training session, from the end the
 * machine reports to the record it deposits, and who reads it.
 *
 * - provisional first: when the machine reports the end, from the 1 Hz
 *   telemetry and the verdict events it sent;
 * - final then: once the three files of the record the summary reads are
 *   verified, it replaces the provisional one for good;
 * - both idempotent; an unreadable record leaves the provisional summary,
 *   with the reason;
 * - read under the rights of the session only (the matrix holds every role;
 *   here, the history of a rider across machines and organisations).
 *
 * Everything runs in memory with `convex-test`.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { makeFunctionReference } from "convex/server";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { sha256 } from "./lib/recordStorage";
import { endedBeforeStart } from "./sessionReports";
import { ARMING_REFUSAL_CODES } from "./training";
import {
  addSession,
  as,
  modules as productionModules,
  NOW,
  seedWorld,
  type World,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

const modules = {
  ...productionModules,
  "./recordStorage.fixtures.ts": () => import("./recordStorage.fixtures"),
};
const nativeUploadType = makeFunctionReference<
  "mutation",
  { storageId: Id<"_storage">; contentType: string },
  null
>("recordStorage.fixtures:nativeUploadType");

const settle = (w: World) => w.t.finishAllScheduledFunctions(vi.runAllTimers);

/** The machine dates `t = 0` of its session here, on its own clock. */
const MACHINE_START = NOW - 3_600_000;

/** An active auto session of `patient` on `machine`, zone 130-150 bpm. */
async function activeSession(
  w: World,
  fields: { machineId?: Id<"machines">; userId?: Id<"users"> } = {},
) {
  const sessionId = await addSession(w, {
    machineId: fields.machineId ?? w.machine,
    userId: fields.userId ?? w.patient,
    status: "active",
    kind: "auto",
  });
  await w.t.run((ctx) =>
    ctx.db.patch(sessionId, {
      zoneLowBpm: 130,
      zoneHighBpm: 150,
      profileName: "Endurance",
    }),
  );
  return sessionId;
}

/** Telemetry at 1 Hz, as the machine sends it: `bpm` absent when not confirmed. */
async function sendTelemetry(
  w: World,
  sessionId: Id<"sessions">,
  bpms: (number | null)[],
  machineId: Id<"machines"> = w.machine,
) {
  await w.t.mutation(internal.training.storeTelemetry, {
    machineId,
    sessionId,
    points: bpms.map((bpm, second) => ({
      t: MACHINE_START + second * 1000,
      elapsedS: second,
      phase: "hold",
      ...(bpm === null ? {} : { bpm }),
      motorRpm: 900 + second,
      outputRpm: 18 + second / 100,
      setpointMotorRpm: 900,
      gLoad: 0.5 + second / 1000,
      safetyAction: "none",
    })),
  });
}

async function end(
  w: World,
  sessionId: Id<"sessions">,
  machineId: Id<"machines"> = w.machine,
) {
  await w.t.mutation(internal.training.endTrainingSession, {
    machineId,
    sessionId,
    failed: false,
    reason: "programme_complete",
  });
  await settle(w);
}

const summaryOf = async (
  w: World,
  sessionId: Id<"sessions">,
  actor = "patient" as const,
) => {
  const result = await as(w.t, actor).query(api.sessionReports.getSummary, {
    sessionId,
  });
  if (result !== null && "absent" in result)
    throw new Error("Expected a computed summary");
  return result;
};

// --- The deposited record ------------------------------------------------------

const HEADER =
  "t,state,mode,phase,drive_state,sim_state,setpoint_motor_rpm,lfrd_motor_rpm,measured_motor_rpm,measured_fresh,output_rpm,hertz,g_reference,g_leg_tip,setpoint_output_rpm,setpoint_g_leg_tip,manual_target_motor_rpm,hr_true,hr_live,target_bpm,safety_action,safety_rule,output_enabled,silent,current_a,hr_raw,hr_confirmed,hr_quality,drive_status_word";

/** `ticks.csv` of a 2 s session at 5 Hz: confirmed 140 bpm, g 0.6 and 0.97. */
function ticks(): string {
  const rows = [HEADER];
  for (let i = 0; i <= 10; i++) {
    const t = (i * 0.2).toFixed(1);
    rows.push(
      `${t},running,auto,hold,OPERATION_ENABLED,,900,900,900,True,18.07,32.6,0.6,0.97,18.07,0.97,0,,140,,NONE,,True,False,,140,140,good,1`,
    );
  }
  return rows.join("\n") + "\n";
}

const MANIFEST = JSON.stringify({
  schema_version: 2,
  ended_at: "2026-10-14T10:30:00Z",
  files: ["manifest.json", "ticks.csv", "events.jsonl", "checksums.sha256"],
  kind: "auto",
  profile: { zone_low_bpm: 130, zone_high_bpm: 150, min_run_rpm: 20 },
  end_reason: "programme_complete",
  geometry: null,
});

const EVENTS =
  JSON.stringify({ t: 0, kind: "phase", detail: "hold", actor: "system" }) +
  "\n";

/** Deposit one file of the record through the machine's reservation and commit. */
async function deposit(
  w: World,
  sessionId: Id<"sessions">,
  path: string,
  text: string,
) {
  const bytes = new TextEncoder().encode(text);
  const args = {
    machineId: w.machine,
    sessionId,
    path,
    sha256: await sha256(bytes.buffer),
    size: bytes.byteLength,
  };
  const reservation = await w.t.mutation(
    internal.recordStorage.requestUpload,
    args,
  );
  if (!("contentType" in reservation) || !reservation.contentType)
    throw Error("No upload reservation");
  const contentType: string = reservation.contentType;
  const storageId = await w.t.run((ctx) =>
    ctx.storage.store(new Blob([bytes], { type: contentType })),
  );
  await w.t.mutation(nativeUploadType, { storageId, contentType });
  await w.t.mutation(internal.recordStorage.commit, { ...args, storageId });
  await settle(w);
}

async function depositRecord(
  w: World,
  sessionId: Id<"sessions">,
  files: { manifest?: string; ticks?: string; events?: string } = {},
) {
  await deposit(w, sessionId, "manifest.json", files.manifest ?? MANIFEST);
  await deposit(w, sessionId, "ticks.csv", files.ticks ?? ticks());
  await deposit(w, sessionId, "events.jsonl", files.events ?? EVENTS);
}

const summaryRows = (w: World) =>
  w.t.run((ctx) => ctx.db.query("session_summaries_v2").collect());

describe("ANH-234 pre-start endings and absent summary isolation", () => {
  it("recognises only the three known pre-start endings of failed sessions", () => {
    for (const endCode of ARMING_REFUSAL_CODES)
      expect(endedBeforeStart({ status: "failed", endCode })).toBe(true);
    for (const endReason of [
      "Cancelled before start by Synthetic",
      "refusee par la machine : busy",
    ])
      expect(endedBeforeStart({ status: "failed", endReason })).toBe(true);
    for (const session of [
      { status: "completed", endReason: "Cancelled before start by Synthetic" },
      { status: "active", endCode: ARMING_REFUSAL_CODES[0] },
      { status: "failed", endReason: "machine failure" },
      { status: "failed" },
      {
        status: "failed",
        endCode: "machine_failure",
        endReason: "Cancelled before start by Synthetic",
      },
      { status: "completed", endReason: "programme_complete" },
    ] as const)
      expect(endedBeforeStart(session)).toBe(false);
  });

  it.each(["not_started", "not_computed"] as const)(
    "returns %s only to every reader and null to every non-reader",
    async (absent) => {
      const w = await seedWorld(modules);
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "failed",
        kind: "auto",
      });
      if (absent === "not_started")
        await w.t.run((ctx) =>
          ctx.db.patch(sessionId, {
            endReason: "Cancelled before start by Synthetic",
          }),
        );
      for (const actor of ["patient", "manager", "orgAdmin", "admin"] as const)
        expect(
          await as(w.t, actor).query(api.sessionReports.getSummary, {
            sessionId,
          }),
        ).toEqual({ absent });
      for (const actor of [
        "otherPatient",
        "otherManager",
        "orgBAdmin",
        "orgBPatient",
        "orgBManager",
        "stranger",
      ] as const)
        expect(
          await as(w.t, actor).query(api.sessionReports.getSummary, {
            sessionId,
          }),
        ).toBeNull();
      const history = await as(w.t, "patient").query(
        api.sessionReports.riderHistory,
        { userId: w.patient },
      );
      expect(history).toMatchObject([
        { sessionId, summary: null, notStarted: absent === "not_started" },
      ]);
      await w.t.run((ctx) => ctx.db.delete(sessionId));
      expect(
        await as(w.t, "patient").query(api.sessionReports.getSummary, {
          sessionId,
        }),
      ).toBeNull();
    },
  );
});

describe("ANH-89 provisional summary, from the telemetry", () => {
  it.each([10_000, 10_001])(
    "reports event truncation only with events beyond twenty pages: %i",
    async (count) => {
      const w = await seedWorld(modules);
      const sessionId = await activeSession(w);
      await sendTelemetry(w, sessionId, [100, 140]);
      await w.t.run(async (ctx) => {
        for (let seq = 0; seq < count; seq++)
          await ctx.db.insert("training_events", {
            organizationId: w.orgA,
            machineId: w.machine,
            sessionId,
            seq,
            t: MACHINE_START + 1000,
            kind: "verdict",
            detail: seq === 10_000 ? "beyond_limit" : "hr_stale",
            actor: "system",
          });
      });
      await end(w, sessionId);
      const figures = (await summaryOf(w, sessionId))!.figures;
      expect(figures.warnings).toEqual(
        count > 10_000 ? ["events_truncated"] : [],
      );
      expect(figures.verdicts.map(({ rule }) => rule)).toEqual(["hr_stale"]);
    },
    // Inserting 10,000 rows and reading 20 pages needs extra time under CI coverage.
    60_000,
  );

  it("ignores telemetry points whose elapsed time goes backwards or stays equal", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await sendTelemetry(w, sessionId, [140, 140, 140, 140, 140]);
    await w.t.run(async (ctx) => {
      const points = await ctx.db
        .query("training_telemetry")
        .withIndex("by_session_and_t", (q) => q.eq("sessionId", sessionId))
        .collect();
      const elapsed = [0, 10, 5, 6, 10];
      for (let i = 0; i < points.length; i++)
        await ctx.db.patch(points[i]._id, {
          elapsedS: elapsed[i],
          ...(i > 1 ? { bpm: 200, motorRpm: 9999, gLoad: 9 } : {}),
        });
    });
    await end(w, sessionId);
    expect((await summaryOf(w, sessionId))?.figures).toMatchObject({
      samples: 1,
      durationS: 10,
      heartRate: { minBpm: 140, maxBpm: 140, samples: 1 },
      zone: { inZoneS: 10, aboveZoneS: 0, belowZoneS: 0 },
      maxMotorRpm: 901,
      reference: { maxG: 0.501 },
      warnings: ["ticks_out_of_order"],
    });
  });

  it("is computed when the machine reports the end, and marked provisional", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    // 0: before the start; 1-4: 140, 151, none, 129.
    await sendTelemetry(w, sessionId, [100, 140, 151, null, 129]);
    await w.t.mutation(internal.training.storeEvents, {
      machineId: w.machine,
      sessionId,
      events: [
        {
          seq: 0,
          t: MACHINE_START + 2000,
          kind: "verdict",
          detail: "hr_hard_max (REDUCE): 151 bpm",
          actor: "system",
        },
        {
          seq: 1,
          t: MACHINE_START + 3000,
          kind: "verdict",
          detail: "cleared",
          actor: "system",
        },
        {
          seq: 2,
          t: MACHINE_START + 3500,
          kind: "phase",
          detail: "cooldown",
          actor: "system",
        },
      ],
    });
    expect(
      await as(w.t, "patient").query(api.sessionReports.getSummary, { sessionId }),
    ).toEqual({ absent: "not_computed" });

    await end(w, sessionId);

    const summary = await summaryOf(w, sessionId);
    expect(summary).toMatchObject({
      provisional: true,
      computedAt: NOW,
      recordProblem: null,
    });
    expect(summary?.figures).toEqual({
      durationS: 4,
      samples: 4,
      heartRate: { minBpm: 129, meanBpm: 140, maxBpm: 151, samples: 3 },
      zone: {
        lowBpm: 130,
        highBpm: 150,
        inZoneS: 1,
        aboveZoneS: 1,
        belowZoneS: 1,
      },
      maxOutputRpm: 18.04,
      maxMotorRpm: 904,
      reference: { maxG: 0.504, maxGRate: null, radiusM: null },
      legTip: { maxG: null, maxGRate: null, radiusM: null, measured: null },
      verdicts: [{ rule: "hr_hard_max", action: "REDUCE", firstAtS: 2 }],
      verdictsOmitted: 0,
      endReason: "programme_complete",
      endRule: null,
      warnings: [],
    });
  });

  it("has no zone for a manual session, and nothing but its end without telemetry", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "manual",
    });
    await end(w, sessionId);
    const summary = await summaryOf(w, sessionId);
    expect(summary?.provisional).toBe(true);
    expect(summary?.figures).toMatchObject({
      durationS: null,
      samples: 0,
      heartRate: null,
      zone: null,
      verdicts: [],
      endReason: "programme_complete",
    });
  });

  it("computes nothing for a session that is not over, nor for one of the retired recording mode", async () => {
    const w = await seedWorld(modules);
    const running = await activeSession(w);
    const legacy = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    for (const sessionId of [running, legacy])
      await w.t.action(internal.sessionReports.summarizeTelemetry, {
        sessionId,
      });
    await w.t.mutation(internal.sessionReports.storeSummary, {
      sessionId: running,
      source: "telemetry",
      figures: (await placeholderFigures(w)).figures,
    });
    expect(await summaryRows(w)).toEqual([]);
  });
});

/** The figures of a finished session, to write by hand. */
async function placeholderFigures(w: World) {
  const sessionId = await activeSession(w);
  await end(w, sessionId);
  const summary = await summaryOf(w, sessionId);
  if (!summary) throw Error("No summary");
  await w.t.run(async (ctx) => {
    for (const row of await ctx.db.query("session_summaries_v2").collect())
      await ctx.db.delete(row._id);
  });
  return summary;
}

describe("ANH-89 final summary, from the deposited record", () => {
  it.each(["manifest", "ticks", "events"] as const)(
    "records an unexpected %s read failure without logging file contents or replacing a final summary",
    async (file) => {
      const w = await seedWorld(modules);
      const sessionId = await activeSession(w);
      await sendTelemetry(w, sessionId, [100, 140, 140]);
      await end(w, sessionId);
      const provisional = await summaryOf(w, sessionId);
      await depositRecord(w, sessionId);
      const final = await summaryOf(w, sessionId);
      const row = (await summaryRows(w))[0];
      await w.t.run((ctx) =>
        ctx.db.patch(row._id, {
          source: "telemetry",
          figures: provisional!.figures,
        }),
      );
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const method = file === "ticks" ? "slice" : "arrayBuffer";
      const original = Blob.prototype[method];
      const failure = vi
        .spyOn(Blob.prototype, method)
        .mockImplementation(function (this: Blob, ...args: unknown[]) {
          if (
            (file === "events" && this.size === new Blob([EVENTS]).size) ||
            file !== "events"
          )
            throw Error("private file content: 186 bpm");
          return Reflect.apply(original, this, args);
        });
      await expect(
        w.t.action(internal.sessionReports.summarizeRecord, { sessionId }),
      ).resolves.toBeNull();
      expect(await summaryOf(w, sessionId)).toEqual({
        ...provisional,
        recordProblem: "summary_failed",
      });
      expect(warn.mock.calls).toEqual([
        [{ sessionId, code: "summary_failed" }],
      ]);
      failure.mockRestore();
      await w.t.action(internal.sessionReports.summarizeRecord, { sessionId });
      const restored = await summaryOf(w, sessionId);
      expect(restored).toEqual(final);
      vi.spyOn(Blob.prototype, "slice").mockImplementation(() => {
        throw Error("private file content: 187 bpm");
      });
      await expect(
        w.t.action(internal.sessionReports.summarizeRecord, { sessionId }),
      ).resolves.toBeNull();
      expect(await summaryOf(w, sessionId)).toEqual(restored);
      expect(warn.mock.calls).toEqual([
        [{ sessionId, code: "summary_failed" }],
        [{ sessionId, code: "summary_failed" }],
      ]);
    },
  );

  it.each([
    ["header only", HEADER + "\n"],
    [
      "ticks at or before the start",
      ticks().split("\n").slice(0, 2).join("\n") + "\n",
    ],
  ])(
    "keeps the provisional figures when the record has %s",
    async (_name, csv) => {
      const w = await seedWorld(modules);
      const sessionId = await activeSession(w);
      await sendTelemetry(w, sessionId, [100, 140, 140]);
      await end(w, sessionId);
      const provisional = await summaryOf(w, sessionId);
      await depositRecord(w, sessionId, { ticks: csv });
      expect(await summaryOf(w, sessionId)).toEqual({
        ...provisional,
        provisional: true,
        recordProblem: "no_session_ticks",
      });
    },
  );

  it("replaces the provisional summary once manifest, ticks and events are verified", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await sendTelemetry(w, sessionId, [100, 140, 140]);
    await end(w, sessionId);
    expect((await summaryOf(w, sessionId))?.provisional).toBe(true);

    await deposit(w, sessionId, "manifest.json", MANIFEST);
    await deposit(w, sessionId, "ticks.csv", ticks());
    // Two of three: still provisional.
    expect((await summaryOf(w, sessionId))?.provisional).toBe(true);
    await deposit(w, sessionId, "events.jsonl", EVENTS);

    const summary = await summaryOf(w, sessionId);
    expect(summary?.provisional).toBe(false);
    expect(summary?.figures).toMatchObject({
      durationS: 2,
      samples: 10,
      heartRate: { minBpm: 140, maxBpm: 140, samples: 10 },
      zone: { inZoneS: 2, aboveZoneS: 0, belowZoneS: 0 },
      reference: { maxG: 0.6, maxGRate: 0 },
      legTip: { maxG: 0.97, maxGRate: 0 },
      endReason: "programme_complete",
    });
    expect(await summaryRows(w)).toHaveLength(1);
  });

  it("is never replaced by the provisional summary, and computes the same twice", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await end(w, sessionId);
    await depositRecord(w, sessionId);
    const final = await summaryOf(w, sessionId);
    expect(final?.provisional).toBe(false);

    vi.setSystemTime(NOW + 60_000);
    await w.t.action(internal.sessionReports.summarizeTelemetry, { sessionId });
    expect(await summaryOf(w, sessionId)).toEqual(final);
    await w.t.mutation(internal.sessionReports.storeSummary, {
      sessionId,
      source: "telemetry",
      figures: { ...final!.figures, durationS: 1 },
    });
    expect(await summaryOf(w, sessionId)).toEqual(final);

    await w.t.action(internal.sessionReports.summarizeRecord, { sessionId });
    expect(await summaryOf(w, sessionId)).toEqual({
      ...final,
      computedAt: NOW + 60_000,
    });
    expect(await summaryRows(w)).toHaveLength(1);
  });

  it("keeps the provisional summary, with the reason, when the record cannot be read", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await sendTelemetry(w, sessionId, [100, 140]);
    await end(w, sessionId);
    await depositRecord(w, sessionId, { ticks: "not,a,tick,header\n" });
    const summary = await summaryOf(w, sessionId);
    expect(summary).toMatchObject({
      provisional: true,
      recordProblem: "invalid_ticks",
    });
    expect(summary?.figures.samples).toBe(1);
  });

  it("is not computed for a record deleted before the computation", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await end(w, sessionId);
    await depositRecord(w, sessionId);
    const final = await summaryOf(w, sessionId);
    await w.t.run((ctx) => ctx.db.patch(sessionId, { recordDeleted: true }));
    vi.setSystemTime(NOW + 60_000);
    await w.t.action(internal.sessionReports.summarizeRecord, { sessionId });
    // The summary stays, as the session and its telemetry do.
    expect(await summaryOf(w, sessionId)).toEqual(final);
  });

  it("notes nothing on a session that has no provisional summary", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
      kind: "auto",
    });
    await w.t.mutation(internal.sessionReports.noteRecordProblem, {
      sessionId,
      problem: "missing_file",
    });
    expect(await summaryRows(w)).toEqual([]);
  });
});

describe("ANH-234 time in zone counted by the console", () => {
  /** The console counted 1.6 s in zone and 0.2 s above; the ticks say 2 s in zone. */
  const COUNTED = JSON.stringify({
    ...JSON.parse(MANIFEST),
    end_observation: {
      t: 2,
      runtime_state: "finished",
      zone_in_s: 1.6000000000000003,
      zone_above_s: 0.2,
      zone_below_s: 0,
    },
  });

  it("is what the final summary, its share of the duration and the history show", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await sendTelemetry(w, sessionId, [100, 140, 140]);
    await end(w, sessionId);
    const provisional = await summaryOf(w, sessionId);
    expect(provisional?.provisional).toBe(true);
    expect(provisional?.figures.zone).not.toHaveProperty("source");

    await depositRecord(w, sessionId, { manifest: COUNTED });

    const summary = await summaryOf(w, sessionId);
    expect(summary?.provisional).toBe(false);
    expect(summary?.figures.durationS).toBe(2);
    expect(summary?.figures.zone).toEqual({
      lowBpm: 130,
      highBpm: 150,
      inZoneS: 1.6,
      aboveZoneS: 0.2,
      belowZoneS: 0,
      source: "console",
    });
    const history = await as(w.t, "patient").query(
      api.sessionReports.riderHistory,
      { userId: w.patient },
    );
    expect(history[0].summary).toMatchObject({
      provisional: false,
      durationS: 2,
      inZoneS: 1.6,
    });
  });

  it("is counted on the ticks again for a record written before the console wrote it", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await end(w, sessionId);
    await depositRecord(w, sessionId);
    expect((await summaryOf(w, sessionId))?.figures.zone).toEqual({
      lowBpm: 130,
      highBpm: 150,
      inZoneS: 2,
      aboveZoneS: 0,
      belowZoneS: 0,
      source: "ticks",
    });
  });

  it("leaves valid a summary stored before the zone said who counted it", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await end(w, sessionId);
    await depositRecord(w, sessionId);
    const final = await summaryOf(w, sessionId);
    const zone = final!.figures.zone!;
    const older = {
      lowBpm: zone.lowBpm,
      highBpm: zone.highBpm,
      inZoneS: zone.inZoneS,
      aboveZoneS: zone.aboveZoneS,
      belowZoneS: zone.belowZoneS,
    };
    await w.t.run(async (ctx) => {
      const row = await ctx.db.query("session_summaries_v2").unique();
      await ctx.db.patch(row!._id, {
        figures: { ...row!.figures, zone: older },
      });
    });
    const read = await summaryOf(w, sessionId);
    expect(read?.figures.zone).toEqual(older);
    expect(read?.figures.zone).not.toHaveProperty("source");
  });
});

describe("ANH-89 summary and history: who reads them", () => {
  it("serves the summary under the rights of its session only", async () => {
    const w = await seedWorld(modules);
    const sessionId = await activeSession(w);
    await end(w, sessionId);
    for (const actor of ["patient", "manager", "orgAdmin", "admin"] as const)
      expect(
        await as(w.t, actor).query(api.sessionReports.getSummary, {
          sessionId,
        }),
      ).not.toBeNull();
    for (const actor of [
      "otherManager",
      "otherPatient",
      "stranger",
      "orgBAdmin",
      "orgBManager",
      "orgBPatient",
    ] as const)
      expect(
        await as(w.t, actor).query(api.sessionReports.getSummary, {
          sessionId,
        }),
      ).toBeNull();
  });

  it("lists a rider's finished sessions, newest first, with their summary or none yet", async () => {
    const w = await seedWorld(modules);
    const first = await activeSession(w);
    await sendTelemetry(w, first, [100, 140, 140]);
    await end(w, first);
    vi.setSystemTime(NOW + 86_400_000);
    const second = await activeSession(w);
    await w.t.run((ctx) =>
      ctx.db.patch(second, { startedAt: NOW + 86_400_000 }),
    );
    await end(w, second);
    await depositRecord(w, second);
    // Neither over nor of training: not in a history.
    await activeSession(w);
    const legacy = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    // Over, with no summary yet (cancelled before the start).
    const cancelled = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "failed",
      kind: "auto",
    });
    await w.t.run((ctx) => ctx.db.patch(cancelled, { startedAt: NOW - 1000 }));

    const history = await as(w.t, "patient").query(
      api.sessionReports.riderHistory,
      { userId: w.patient },
    );
    expect(history.map((row) => row.sessionId)).toEqual([
      second,
      first,
      cancelled,
    ]);
    expect(history.map((row) => row.sessionId)).not.toContain(legacy);
    expect(history[0]).toMatchObject({
      status: "completed",
      kind: "auto",
      machineName: "Synthetic machine",
      profileName: "Endurance",
      summary: {
        provisional: false,
        durationS: 2,
        inZoneS: 2,
        zoneLowBpm: 130,
        zoneHighBpm: 150,
        meanBpm: 140,
        maxBpm: 140,
        maxGReference: 0.6,
      },
    });
    expect(history[1].summary).toMatchObject({
      provisional: true,
      durationS: 2,
      inZoneS: 2,
    });
    expect(history[2]).toMatchObject({ status: "failed", summary: null });
  });

  it("lists to a gestionnaire only the rider's sessions on the machines they manage", async () => {
    const w = await seedWorld(modules);
    const own = await activeSession(w);
    await end(w, own);
    // The same rider on a machine of another gestionnaire.
    const elsewhere = await activeSession(w, { machineId: w.otherMachine });
    await end(w, elsewhere, w.otherMachine);

    const seen = async (actor: "manager" | "otherManager" | "orgAdmin") =>
      (
        await as(w.t, actor).query(api.sessionReports.riderHistory, {
          userId: w.patient,
        })
      ).map((row) => row.sessionId);
    expect(await seen("manager")).toEqual([own]);
    expect(await seen("otherManager")).toEqual([elsewhere]);
    expect((await seen("orgAdmin")).sort()).toEqual([own, elsewhere].sort());
  });
});
