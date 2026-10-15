/**
 * ANH-89: the figures of a session summary, on fixture records written as the
 * console writes them (docs/enregistrement.md, schema 2). Each figure is
 * checked against its definition in `convex/lib/sessionSummary.ts`.
 */
import { describe, expect, it } from "vitest";
import {
  csvFields,
  DEFAULT_MIN_RUN_MOTOR_RPM,
  forEachLine,
  manifestContext,
  MAX_LINE_CHARS,
  MAX_TEXT_CHARS,
  MAX_VERDICTS,
  parseVerdict,
  summarizeRecordFiles,
  type SummaryFigures,
} from "./lib/sessionSummary";

/** The header of `ticks.csv`, in the order of `src.record.rows.Row`. */
const HEADER =
  "t,state,mode,phase,drive_state,sim_state,setpoint_motor_rpm,lfrd_motor_rpm,measured_motor_rpm,measured_fresh,output_rpm,hertz,g_reference,g_leg_tip,setpoint_output_rpm,setpoint_g_leg_tip,manual_target_motor_rpm,hr_true,hr_live,target_bpm,safety_action,safety_rule,output_enabled,silent,current_a,hr_raw,hr_confirmed,hr_quality,drive_status_word";
const COLUMNS = HEADER.split(",");

type Tick = {
  t: number;
  hr?: number | null;
  motor?: number;
  output?: number;
  gRef?: number;
  gLeg?: number;
  action?: string;
  rule?: string | null;
  state?: string;
  fresh?: boolean;
  silent?: boolean;
  drive?: string;
};

/** One line of `ticks.csv`: a running, judged tick unless told otherwise. */
function tick({
  t,
  hr = 140,
  motor = 900,
  output = 18,
  gRef = 0.5,
  gLeg = 0.8,
  action = "NONE",
  rule = null,
  state = "running",
  fresh = true,
  silent = false,
  drive = "OPERATION_ENABLED",
}: Tick): string {
  const value: Record<string, string> = {
    t: String(t),
    state,
    mode: "auto",
    phase: "hold",
    drive_state: drive,
    sim_state: "",
    setpoint_motor_rpm: String(motor),
    lfrd_motor_rpm: String(motor),
    measured_motor_rpm: String(motor),
    measured_fresh: fresh ? "True" : "False",
    output_rpm: String(output),
    hertz: "30.0",
    g_reference: String(gRef),
    g_leg_tip: String(gLeg),
    setpoint_output_rpm: String(output),
    setpoint_g_leg_tip: String(gLeg),
    manual_target_motor_rpm: "0",
    hr_true: "",
    hr_live: hr === null ? "" : String(hr),
    target_bpm: "",
    safety_action: action,
    safety_rule: rule ?? "",
    output_enabled: "True",
    silent: silent ? "True" : "False",
    current_a: "",
    hr_raw: hr === null ? "" : String(hr),
    hr_confirmed: hr === null ? "" : String(hr),
    hr_quality: hr === null ? "no_signal" : "good",
    drive_status_word: "1",
  };
  return COLUMNS.map((column) => value[column]).join(",");
}

const ticksCsv = (ticks: Tick[]) =>
  [HEADER, ...ticks.map(tick)].join("\n") + "\n";

const AUTO_PROFILE = {
  zone_low_bpm: 130,
  zone_high_bpm: 150,
  min_run_rpm: 20,
};

function manifest(fields: Record<string, unknown> = {}): string {
  return JSON.stringify({
    schema_version: 2,
    ended_at: "2026-10-14T10:30:00Z",
    files: ["manifest.json", "ticks.csv", "events.jsonl", "checksums.sha256"],
    kind: "auto",
    profile: AUTO_PROFILE,
    end_reason: "programme_complete",
    geometry: null,
    ...fields,
  });
}

const event = (kind: string, detail: string, t = 1) =>
  JSON.stringify({ t, kind, detail, actor: "system" });

async function summarize(files: {
  manifest?: string;
  ticks: string;
  events?: string;
}): Promise<SummaryFigures> {
  const outcome = await summarizeRecordFiles({
    manifest: new Blob([files.manifest ?? manifest()]),
    ticks: new Blob([files.ticks]),
    events: new Blob([files.events ?? ""]),
    maxFileBytes: 16 * 1024 * 1024,
  });
  if ("problem" in outcome) throw new Error(outcome.problem);
  return outcome.figures;
}

/** Ticks every 0.2 s from `from` (excluded) to `to` (included). */
function every(from: number, to: number, fields: Omit<Tick, "t"> = {}) {
  const ticks: Tick[] = [];
  for (let i = 1; from + i * 0.2 <= to + 1e-9; i++)
    ticks.push({ ...fields, t: Math.round((from + i * 0.2) * 1000) / 1000 });
  return ticks;
}

describe("summary of a record: duration and heart rate", () => {
  it("counts the session ticks only, on the record's monotonic clock", async () => {
    const figures = await summarize({
      ticks: ticksCsv([
        { t: -0.4, hr: 60, motor: 0, output: 0, gRef: 0, gLeg: 0 },
        { t: 0, hr: 61, motor: 0, output: 0, gRef: 0, gLeg: 0 },
        { t: 0.2, hr: 120 },
        { t: 0.4, hr: 150 },
        { t: 0.6, hr: 131 },
      ]),
    });
    expect(figures.durationS).toBe(0.6);
    expect(figures.samples).toBe(3);
    expect(figures.heartRate).toEqual({
      minBpm: 120,
      meanBpm: 133.7,
      maxBpm: 150,
      samples: 3,
    });
  });

  it("has no heart rate and no time in zone when no tick carries a confirmed rate", async () => {
    const figures = await summarize({
      ticks: ticksCsv([{ t: 0, hr: null }, ...every(0, 2, { hr: null })]),
    });
    expect(figures.heartRate).toBeNull();
    expect(figures.zone).toEqual({
      lowBpm: 130,
      highBpm: 150,
      inZoneS: 0,
      aboveZoneS: 0,
      belowZoneS: 0,
    });
    expect(figures.durationS).toBe(2);
  });

  it("has no duration when the record holds no session tick", async () => {
    const figures = await summarize({ ticks: ticksCsv([{ t: 0 }]) });
    expect(figures).toMatchObject({
      durationS: null,
      samples: 0,
      heartRate: null,
      maxOutputRpm: null,
      maxMotorRpm: null,
      reference: { maxG: null, maxGRate: null },
      legTip: { maxG: null, maxGRate: null },
    });
  });
});

describe("summary of a record: time in zone, as the console counts it", () => {
  it("adds the time since the previous tick to the band of each tick's rate, bounds in the zone", async () => {
    const figures = await summarize({
      ticks: ticksCsv([
        { t: 0, hr: 100 },
        { t: 0.2, hr: 129 }, // below: 0.2
        { t: 0.4, hr: 130 }, // in: 0.2 (the lower bound is in)
        { t: 1.4, hr: 150 }, // in: 1.0 (a late tick counts its whole interval)
        { t: 1.6, hr: 151 }, // above: 0.2
        { t: 1.8, hr: null }, // nowhere
        { t: 2.0, hr: 140 }, // in: 0.2
      ]),
    });
    expect(figures.zone).toEqual({
      lowBpm: 130,
      highBpm: 150,
      inZoneS: 1.4,
      aboveZoneS: 0.2,
      belowZoneS: 0.2,
    });
  });

  it("counts no interval for the first tick when the record has none before it", async () => {
    const figures = await summarize({
      ticks: ticksCsv([
        { t: 0.2, hr: 140 },
        { t: 0.4, hr: 140 },
      ]),
    });
    expect(figures.zone?.inZoneS).toBe(0.2);
  });

  it("has no zone without a programme: a manual session", async () => {
    const figures = await summarize({
      manifest: manifest({ kind: "manual", profile: null }),
      ticks: ticksCsv([{ t: 0 }, ...every(0, 1)]),
    });
    expect(figures.zone).toBeNull();
    expect(figures.heartRate?.samples).toBe(5);
  });
});

describe("summary of a record: speed and g at both radii", () => {
  it("takes the maximum speed of the arm and the motor, whichever the direction", async () => {
    const figures = await summarize({
      ticks: ticksCsv([
        { t: 0.2, motor: 900, output: 18.07 },
        { t: 0.4, motor: -1344, output: 26.9937 },
        { t: 0.6, motor: 1000, output: 20.08 },
      ]),
    });
    expect(figures.maxOutputRpm).toBe(26.99);
    expect(figures.maxMotorRpm).toBe(1344);
  });

  it("reads g at the reference radius and at the foot tip, with the geometry the manifest declares", async () => {
    const figures = await summarize({
      manifest: manifest({
        geometry: {
          reference_radius_m: 1.5,
          leg_tip_radius_m: 2.4254,
          arm_tip_radius_m: 2.6,
          capsule_near_radius_m: 1.2,
          capsule_far_radius_m: 2.5,
          counterweight_radius_m: 1,
          leg_tip_measured: false,
          gear_ratio: 49.79,
          nominal_motor_rpm: 1380,
          base_hz: 50,
        },
      }),
      ticks: ticksCsv([
        { t: 0.2, gRef: 0.6379, gLeg: 1.0315 },
        { t: 0.4, gRef: 0.5, gLeg: 0.81 },
      ]),
    });
    expect(figures.reference).toMatchObject({ maxG: 0.6379, radiusM: 1.5 });
    expect(figures.legTip).toMatchObject({
      maxG: 1.0315,
      radiusM: 2.4254,
      measured: false,
    });
  });

  it("says nothing of the radii when the manifest has no geometry (the console)", async () => {
    const figures = await summarize({
      ticks: ticksCsv([{ t: 0.2, gRef: 1.1, gLeg: 1.7 }]),
    });
    expect(figures.reference.radiusM).toBeNull();
    expect(figures.legTip).toMatchObject({
      maxG: 1.7,
      radiusM: null,
      measured: null,
    });
  });

  it("judges the variation of g over windows of five intervals whose ticks are all judged", async () => {
    // Six judged ticks, 0.2 s apart: g rises by 0.01 at the reference and by
    // 0.02 at the foot tip over 1 s. Then a stop breaks every window it is in.
    const rising = [0, 1, 2, 3, 4, 5].map((i) => ({
      t: 0.2 * (i + 1),
      gRef: 0.5 + 0.002 * i,
      gLeg: 0.8 + 0.004 * i,
    }));
    const figures = await summarize({
      ticks: ticksCsv([
        ...rising,
        { t: 1.4, gRef: 0.9, gLeg: 1.5, action: "QUICK_STOP" },
        { t: 1.6, gRef: 0.1, gLeg: 0.2 },
      ]),
    });
    expect(figures.reference.maxGRate).toBe(0.01);
    expect(figures.legTip.maxGRate).toBe(0.02);
  });

  it.each([
    ["a stale measurement", { fresh: false }],
    ["a silent runtime", { silent: true }],
    ["an emergency action", { action: "GO_SILENT" }],
    ["a drive that is not enabled", { drive: "SWITCHED_ON" }],
    ["a speed under the programme's slowest setpoint", { motor: 19 }],
  ])("does not judge a window that holds %s", async (_what, fields) => {
    const ticks: Tick[] = [0, 1, 2, 3, 4, 5].map((i) => ({
      t: 0.2 * (i + 1),
      gRef: 0.5 + 0.01 * i,
      gLeg: 0.8 + 0.02 * i,
      ...(i === 3 ? fields : {}),
    }));
    const figures = await summarize({ ticks: ticksCsv(ticks) });
    expect(figures.reference.maxGRate).toBeNull();
    expect(figures.legTip.maxGRate).toBeNull();
  });

  it("judges a manual session against the shipped slowest setpoint", async () => {
    const below = DEFAULT_MIN_RUN_MOTOR_RPM - 1;
    const ticks = (motor: number) =>
      [0, 1, 2, 3, 4, 5].map((i) => ({
        t: 0.2 * (i + 1),
        motor,
        gRef: 0.01 * i,
      }));
    const manual = manifest({ kind: "manual", profile: null });
    expect(
      (await summarize({ manifest: manual, ticks: ticksCsv(ticks(below)) }))
        .reference.maxGRate,
    ).toBeNull();
    expect(
      (
        await summarize({
          manifest: manual,
          ticks: ticksCsv(ticks(DEFAULT_MIN_RUN_MOTOR_RPM)),
        })
      ).reference.maxGRate,
    ).toBe(0.05);
  });
});

describe("summary of a record: verdicts and the end", () => {
  it("lists every rule that stood, first seen first, with its most severe action", async () => {
    const figures = await summarize({
      ticks: ticksCsv([
        { t: 0.2 },
        { t: 0.4, rule: "hr_hard_max", action: "REDUCE" },
        { t: 0.6, rule: "hr_hard_max", action: "RAMP_DOWN" },
        { t: 0.8, rule: "tracking_error", action: "FREEZE" },
        { t: 1.0, rule: "hr_hard_max", action: "FREEZE" },
      ]),
    });
    expect(figures.verdicts).toEqual([
      { rule: "hr_hard_max", action: "RAMP_DOWN", firstAtS: 0.4 },
      { rule: "tracking_error", action: "FREEZE", firstAtS: 0.8 },
    ]);
    expect(figures.verdictsOmitted).toBe(0);
    expect(figures.endReason).toBe("programme_complete");
    expect(figures.endRule).toBeNull();
  });

  it("names the rule standing when a safety verdict ended the session", async () => {
    const figures = await summarize({
      manifest: manifest({ end_reason: "safety_verdict" }),
      ticks: ticksCsv([
        { t: 0.2, rule: "hr_rate", action: "FREEZE" },
        { t: 0.4, rule: "hr_critical", action: "QUICK_STOP" },
        { t: 0.6, rule: "hr_critical", action: "QUICK_STOP", state: "ending" },
        { t: 0.8, rule: "comms_lost", action: "QUICK_STOP", state: "finished" },
      ]),
    });
    expect(figures.endReason).toBe("safety_verdict");
    expect(figures.endRule).toBe("hr_critical");
  });

  it("takes the last rule seen when the first tick of the ending carries none", async () => {
    const figures = await summarize({
      manifest: manifest({ end_reason: "safety_verdict" }),
      ticks: ticksCsv([
        { t: 0.2, rule: "session_overrun", action: "RAMP_DOWN" },
        { t: 0.4, state: "ending" },
      ]),
    });
    expect(figures.endRule).toBe("session_overrun");
  });

  it("names no rule for an end that is not a safety verdict", async () => {
    const figures = await summarize({
      manifest: manifest({ end_reason: "emergency_stop" }),
      ticks: ticksCsv([
        {
          t: 0.2,
          rule: "operator_estop",
          action: "QUICK_STOP",
          state: "ending",
        },
      ]),
    });
    expect(figures.endReason).toBe("emergency_stop");
    expect(figures.endRule).toBeNull();
  });

  it(`keeps ${MAX_VERDICTS} rules and counts the others`, async () => {
    const ticks = Array.from({ length: MAX_VERDICTS + 3 }, (_, i) => ({
      t: 0.2 * (i + 1),
      rule: `rule_${i}`,
      action: "FREEZE",
    }));
    const figures = await summarize({
      ticks: ticksCsv([
        ...ticks,
        { t: 100, rule: "rule_33", action: "FREEZE" },
      ]),
    });
    expect(figures.verdicts).toHaveLength(MAX_VERDICTS);
    expect(figures.verdictsOmitted).toBe(3);
  });

  it("says when the record itself says it misses data", async () => {
    const figures = await summarize({
      ticks: ticksCsv([{ t: 0.2 }]),
      events:
        [
          event("phase", "hold"),
          event("warning", "record_degraded: dropped=3 failures=0"),
        ].join("\n") + "\n",
    });
    expect(figures.warnings).toEqual(["record_degraded"]);
  });
});

describe("summary of a record: reading the files", () => {
  it("finds the columns by name, whatever their order, and reads quoted fields", async () => {
    const header = [
      "hr_confirmed",
      "extra",
      ...COLUMNS.filter((c) => c !== "hr_confirmed"),
    ];
    const line = (t: number, hr: number) =>
      header
        .map((column) => {
          if (column === "hr_confirmed") return String(hr);
          if (column === "extra") return '"a, ""quoted"" text"';
          const full = csvFields(tick({ t, hr }));
          return full[COLUMNS.indexOf(column)];
        })
        .join(",");
    const figures = await summarize({
      ticks:
        [header.join(","), line(0.2, 133), line(0.4, 147)].join("\r\n") +
        "\r\n",
    });
    expect(figures.heartRate).toMatchObject({ minBpm: 133, maxBpm: 147 });
    expect(figures.warnings).toEqual([]);
  });

  it("skips and reports the lines that are not ticks or events, and ignores an unfinished last line", async () => {
    const figures = await summarize({
      ticks:
        ticksCsv([{ t: 0.2, hr: 140 }]) +
        "0.4,too,few\n" +
        tick({ t: 0.6, gRef: 0.777 }).replace("0.777", "not-a-number") +
        "\n" +
        tick({ t: 0.8, hr: 99 }),
      events: event("phase", "hold") + "\n{not json\n",
    });
    expect(figures.samples).toBe(1);
    expect(figures.heartRate?.maxBpm).toBe(140);
    expect(figures.warnings).toEqual(["lines_skipped"]);
  });

  it("skips a line longer than any tick", async () => {
    const figures = await summarize({
      ticks: ticksCsv([{ t: 0.2 }]) + "x".repeat(MAX_LINE_CHARS + 1) + "\n",
    });
    expect(figures.samples).toBe(1);
    expect(figures.warnings).toEqual(["lines_skipped"]);
  });

  it.each([
    ["one of the files is missing", { ticks: null }, "missing_file"],
    ["a stream is larger than a deposit", { tooLarge: true }, "too_large"],
    ["the manifest is not JSON", { manifest: "{" }, "invalid_manifest"],
    ["the manifest is not an object", { manifest: "[1]" }, "invalid_manifest"],
    ["the ticks have no header", { ticks: "" }, "invalid_ticks"],
    [
      "the header lacks a column",
      { ticks: "t,state\n0.2,running\n" },
      "invalid_ticks",
    ],
  ])("refuses a record when %s", async (_what, change, problem) => {
    const outcome = await summarizeRecordFiles({
      manifest: new Blob([
        "manifest" in change ? (change.manifest as string) : manifest(),
      ]),
      ticks:
        "ticks" in change
          ? change.ticks === null
            ? null
            : new Blob([change.ticks as string])
          : new Blob([ticksCsv([{ t: 0.2 }])]),
      events: new Blob([""]),
      maxFileBytes: "tooLarge" in change ? 10 : 16 * 1024 * 1024,
    });
    expect(outcome).toEqual({ problem });
  });

  it("refuses a stream that is not UTF-8", async () => {
    const outcome = await summarizeRecordFiles({
      manifest: new Blob([manifest()]),
      ticks: new Blob([HEADER + "\n", new Uint8Array([0xff, 0xfe, 0x0a])]),
      events: new Blob([""]),
      maxFileBytes: 1024 * 1024,
    });
    expect(outcome).toEqual({ problem: "invalid_encoding" });
  });

  it("reads a file slice by slice, a character or a line cut between two slices", async () => {
    const lines: string[] = [];
    const text = "première ligne\nsecond é\n" + "x".repeat(10) + "\nunfinished";
    const result = await forEachLine(
      new Blob([text]),
      (line) => lines.push(line),
      3,
    );
    expect(lines).toEqual(["première ligne", "second é", "x".repeat(10)]);
    expect(result.overlong).toBe(0);
  });

  it("skips a line that grows past the bound across slices", async () => {
    const lines: string[] = [];
    const long = "y".repeat(MAX_LINE_CHARS + 10);
    const result = await forEachLine(
      new Blob([`a\n${long}\nb\n`]),
      (line) => lines.push(line),
      4096,
    );
    expect(lines).toEqual(["a", "b"]);
    expect(result.overlong).toBe(1);
  });
});

describe("summary helpers", () => {
  it("reads what it needs of a manifest, and only what has the expected shape", () => {
    expect(manifestContext({})).toEqual({
      zone: null,
      minRunMotorRpm: DEFAULT_MIN_RUN_MOTOR_RPM,
      endReason: null,
      referenceRadiusM: null,
      legTipRadiusM: null,
      legTipMeasured: null,
    });
    expect(
      manifestContext({
        profile: { zone_low_bpm: "130" },
        geometry: { reference_radius_m: -1 },
        end_reason: "x".repeat(500),
      }),
    ).toMatchObject({
      zone: null,
      referenceRadiusM: null,
      endReason: "x".repeat(MAX_TEXT_CHARS),
    });
    expect(manifestContext("text")).toBeNull();
    expect(manifestContext(null)).toBeNull();
  });

  it("reads the verdict events of the console and of the simulation", () => {
    expect(parseVerdict("hr_critical (QUICK_STOP): 186 bpm above 185")).toEqual(
      {
        rule: "hr_critical",
        action: "QUICK_STOP",
      },
    );
    expect(parseVerdict("hr_critical")).toEqual({
      rule: "hr_critical",
      action: null,
    });
    expect(parseVerdict("cleared")).toBeNull();
    expect(parseVerdict("free text with spaces")).toBeNull();
  });

  it("splits a CSV line as Python's csv module writes it", () => {
    expect(csvFields('a,"b,c","d ""e""",')).toEqual(["a", "b,c", 'd "e"', ""]);
  });
});
