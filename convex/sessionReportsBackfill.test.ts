import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { addSession, modules, NOW, seedWorld, type World } from "./test.setup";
import {
  DEFAULT_BATCH_SIZE,
  MAX_BATCH_SIZE,
  SESSION_DELAY_MS,
} from "./sessionReportsBackfill";
import { ARMING_REFUSAL_CODES } from "./training";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => vi.useRealTimers());

async function historical(
  w: World,
  status: "completed" | "failed" = "completed",
  kind: "auto" | "manual" | undefined = "auto",
) {
  const id = await addSession(w, {
    machineId: w.machine,
    userId: w.patient,
    status,
    kind,
  });
  await w.t.run(async (ctx) => {
    await ctx.db.patch(id, {
      startedAt: NOW - 100_000,
      endedAt: NOW - 90_000,
      zoneLowBpm: 130,
      zoneHighBpm: 150,
    });
    for (const elapsedS of [0, 1, 2])
      await ctx.db.insert("training_telemetry", {
        organizationId: w.orgA,
        machineId: w.machine,
        sessionId: id,
        t: NOW - 100_000 + elapsedS * 1000,
        elapsedS,
        bpm: 140,
        phase: "hold",
        motorRpm: 900,
        outputRpm: 18,
        setpointMotorRpm: 900,
        gLoad: 0.6,
        safetyAction: "none",
      });
  });
  return id;
}

async function verifiedRecord(
  w: World,
  sessionId: Id<"sessions">,
  unverified = false,
) {
  const files = {
    "manifest.json": JSON.stringify({
      schema_version: 2,
      kind: "auto",
      profile: { zone_low_bpm: 130, zone_high_bpm: 150 },
      end_reason: "programme_complete",
      geometry: null,
    }),
    "ticks.csv":
      "t,state,mode,phase,drive_state,sim_state,setpoint_motor_rpm,lfrd_motor_rpm,measured_motor_rpm,measured_fresh,output_rpm,hertz,g_reference,g_leg_tip,setpoint_output_rpm,setpoint_g_leg_tip,manual_target_motor_rpm,hr_true,hr_live,target_bpm,safety_action,safety_rule,output_enabled,silent,current_a,hr_raw,hr_confirmed,hr_quality,drive_status_word\n" +
      [0, 1, 2]
        .map(
          (t) =>
            `${t},running,auto,hold,OPERATION_ENABLED,,900,900,900,True,18,32.6,0.6,0.97,18,0.97,0,,140,,NONE,,True,False,,140,140,good,1\n`,
        )
        .join(""),
    "events.jsonl": "",
  };
  await w.t.run(async (ctx) => {
    for (const [path, text] of Object.entries(files)) {
      const storageId = await ctx.storage.store(new Blob([text]));
      await ctx.db.insert("session_record_files", {
        sessionId,
        machineId: w.machine,
        organizationId: w.orgA,
        path,
        storageId,
        sha256: "synthetic",
        size: text.length,
        uploadedAt: NOW - 90_000,
        verified: !(unverified && path === "events.jsonl"),
      });
    }
  });
}

const rows = (w: World) =>
  w.t.run((ctx) => ctx.db.query("session_summaries_v2").collect());
const jobs = (w: World) =>
  w.t.run((ctx) => ctx.db.system.query("_scheduled_functions").collect());
const settle = (w: World) => w.t.finishAllScheduledFunctions(vi.runAllTimers);

describe("ANH-234 one-off summary backfill", () => {
  it("recovers historical auto, manual and verified records, preserves exclusions, then is idempotent", async () => {
    const w = await seedWorld(modules);
    const auto = await historical(w);
    const manual = await historical(w, "failed", "manual");
    const record = await historical(w);
    const provisional = await historical(w);
    const cancelled = await historical(w, "failed");
    const refusal = await historical(w, "failed");
    const machineRefusal = await historical(w, "failed");
    const legacy = await historical(w);
    const final = await historical(w);
    for (const status of ["pending", "active", "interrupted"] as const)
      await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status,
        kind: "auto",
      });
    await w.t.run(async (ctx) => {
      await ctx.db.patch(cancelled, {
        endReason: "Cancelled before start by Synthetic",
      });
      await ctx.db.patch(refusal, { endCode: ARMING_REFUSAL_CODES[0] });
      await ctx.db.patch(machineRefusal, {
        endReason: "refusee par la machine : busy",
      });
      await ctx.db.patch(legacy, { kind: undefined });
    });
    for (const id of [record, provisional, final]) await verifiedRecord(w, id);
    await w.t.action(internal.sessionReports.summarizeTelemetry, {
      sessionId: provisional,
    });
    await w.t.action(internal.sessionReports.summarizeRecord, {
      sessionId: final,
    });
    const existingFinal = (await rows(w)).find(
      (row) => row.sessionId === final,
    );
    expect(existingFinal?.source).toBe("record");
    const sessionsBefore = await w.t.run((ctx) =>
      ctx.db.query("sessions").collect(),
    );
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {}),
    ).toEqual({ scheduled: 4, done: false });
    await settle(w);
    const summaries = await rows(w);
    expect(summaries).toHaveLength(5);
    for (const id of [auto, manual])
      expect(summaries.find((row) => row.sessionId === id)).toMatchObject({
        source: "telemetry",
        figures: { durationS: 2 },
      });
    for (const id of [record, provisional])
      expect(summaries.find((row) => row.sessionId === id)).toMatchObject({
        source: "record",
        figures: { durationS: 2 },
      });
    expect(summaries.find((row) => row.sessionId === final)).toEqual(
      existingFinal,
    );
    for (const id of [cancelled, refusal, machineRefusal, legacy])
      expect(summaries.some((row) => row.sessionId === id)).toBe(false);
    expect(await w.t.run((ctx) => ctx.db.query("sessions").collect())).toEqual(
      sessionsBefore,
    );
    const beforeJobs = await jobs(w);
    vi.setSystemTime(NOW + 1_000_000);
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {}),
    ).toEqual({ scheduled: 0, done: false });
    await settle(w);
    const newJobs = (await jobs(w)).filter(
      (job) => !beforeJobs.some((before) => before._id === job._id),
    );
    expect(
      newJobs.every(
        (job) => job.name === "sessionReportsBackfill:backfillSummaries",
      ),
    ).toBe(true);
    expect(await rows(w)).toEqual(summaries);
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {
        status: "failed",
      }),
    ).toEqual({ scheduled: 0, done: true });
  });

  it("reads one bounded page per invocation, spaces actions and resumes across both statuses", async () => {
    const w = await seedWorld(modules);
    for (const status of ["completed", "failed"] as const)
      for (let i = 0; i < 5; i++) await historical(w, status);
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {
        batchSize: 2,
      }),
    ).toEqual({ scheduled: 2, done: false });
    const scheduled = await jobs(w);
    const actions = scheduled.filter(
      (job) => job.name === "sessionReports:summarizeTelemetry",
    );
    expect(actions.map((job) => job.scheduledTime - NOW)).toEqual([
      0,
      SESSION_DELAY_MS,
    ]);
    expect(
      scheduled.filter(
        (job) => job.name === "sessionReportsBackfill:backfillSummaries",
      ),
    ).toMatchObject([
      {
        args: [
          { status: "completed", cursor: expect.any(String), batchSize: 2 },
        ],
      },
    ]);
    await settle(w);
    expect(await rows(w)).toHaveLength(10);
    const continuations = (await jobs(w)).filter(
      (job) => job.name === "sessionReportsBackfill:backfillSummaries",
    );
    expect(continuations).toHaveLength(5);
    expect(continuations.map((job) => job.args[0])).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ status: "failed", cursor: null }),
      ]),
    );
    expect((await jobs(w)).every((job) => job.state.kind === "success")).toBe(
      true,
    );
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {
        status: "failed",
      }),
    ).toEqual({ scheduled: 0, done: true });
  });

  it("keeps provisional summaries with a record problem, a deleted record or an unverified file", async () => {
    const w = await seedWorld(modules);
    for (const reason of ["problem", "deleted", "unverified"]) {
      const sessionId = await historical(w, "failed");
      await verifiedRecord(w, sessionId, reason === "unverified");
      await w.t.action(internal.sessionReports.summarizeTelemetry, {
        sessionId,
      });
      if (reason === "problem")
        await w.t.mutation(internal.sessionReports.noteRecordProblem, {
          sessionId,
          problem: "invalid_ticks",
        });
      if (reason === "deleted")
        await w.t.run((ctx) =>
          ctx.db.patch(sessionId, { recordDeleted: true }),
        );
    }
    const before = await rows(w);
    expect(
      await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {
        status: "failed",
      }),
    ).toEqual({ scheduled: 0, done: true });
    expect(await jobs(w)).toEqual([]);
    expect(await rows(w)).toEqual(before);
  });

  it.each([undefined, Number.NaN, 0, 2.9, MAX_BATCH_SIZE + 100])(
    "bounds the optional batch size: %s",
    async (batchSize) => {
      const w = await seedWorld(modules);
      const expected =
        batchSize === undefined || Number.isNaN(batchSize)
          ? DEFAULT_BATCH_SIZE
          : Math.max(1, Math.min(MAX_BATCH_SIZE, Math.floor(batchSize)));
      for (let i = 0; i <= expected; i++) await historical(w, "failed");
      expect(
        await w.t.mutation(internal.sessionReportsBackfill.backfillSummaries, {
          status: "failed",
          ...(batchSize === undefined ? {} : { batchSize }),
        }),
      ).toEqual({ scheduled: expected, done: false });
      await settle(w);
      expect(await rows(w)).toHaveLength(expected + 1);
    },
  );
});
