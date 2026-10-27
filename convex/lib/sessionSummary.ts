/**
 * The summary of a finished training session (ANH-89): what the report of a
 * session and the history of a rider show. Pure computation, no Convex
 * context: `convex/sessionReports.ts` reads the files and the rows, and hands
 * them to this module one line or one point at a time.
 *
 * Two sources, one set of figures:
 *
 * - **record** (final): the session's deposited record, schema 2
 *   (docs/enregistrement.md): `manifest.json`, `ticks.csv` at 5 Hz and
 *   `events.jsonl`, each verified (size and SHA-256) by `recordStorage`.
 * - **telemetry** (provisional): the 1 Hz points the machine sent while the
 *   record was not deposited yet (`training_telemetry`, the first tick of each
 *   second of `ticks.csv`, `raspberry-pi/src/record/upload.py`, `_tick`).
 *
 * Each figure is defined as the machine defines it, and nowhere else:
 *
 * - Session ticks: `t > 0`. `t = 0` is the start command, a tick at 0 is the
 *   last tick before it, and the ticks of the rest that precede the start
 *   have a `t` at or below 0 (docs/enregistrement.md, "Tics, 5 Hz").
 *   A tick or telemetry point at or before the last retained time is ignored
 *   in every figure, counted, and reported as `ticks_out_of_order`.
 * - Duration: the `t` of the last session tick, in seconds, on the monotonic
 *   clock of the record (never a difference of wall-clock dates, which may be
 *   wrong when `clocks.wall_trusted` is false).
 * - Confirmed heart rate: the column `hr_confirmed` (telemetry: `bpm`, the
 *   column `hr_live`, the same value), the rate the console may show as
 *   current: a sample exists, is fresh and its quality allows it
 *   (`TelemetrySnapshot.live_bpm`, raspberry-pi/src/training/types.py;
 *   written by `_row`, raspberry-pi/src/record/session.py). A tick without it
 *   counts in no heart-rate figure. Minimum, arithmetic mean over the ticks
 *   that carry it, maximum.
 * - Time in zone: the rule of `TrainingRuntime._accumulate` (raspberry-pi/
 *   src/training/runtime.py) and `ZoneCounters` (types.py): each tick adds
 *   the time since the previous tick to "in", "above" or "below" according
 *   to its rate against `[zone_low_bpm, zone_high_bpm]`, both bounds in the
 *   zone; a tick without a rate adds to none, so the three do not add up to
 *   the duration. Only a programme has a zone: a manual session has none.
 *   The console classifies the filtered rate of its tracker
 *   (`HeartRateTracker.usable`, hr_control.py), which no record holds, and
 *   writes its three counters at the end of the record (`zone_in_s`,
 *   `zone_above_s`, `zone_below_s` of `end_observation`): the final summary
 *   takes them as they are when all three are there (`zone.source:
 *   "console"`). Otherwise, for a record written before the console wrote
 *   them, it classifies the confirmed rate the record holds (`"ticks"`),
 *   which may differ by a few seconds (1 s out of 1212 s on the
 *   simulation's `hr_critical` scenario). The provisional summary always
 *   classifies its points and says no source.
 * - g at the reference radius and at the foot tip: the columns `g_reference`
 *   and `g_leg_tip`, the centripetal load omega^2 r / 9.80665 at each radius
 *   (`output_rpm_to_g`, raspberry-pi/src/units.py), not the resultant felt
 *   with gravity. The telemetry carries the reference one only (`gLoad`).
 * - Variation of g (g/s): `arm_windows` of simulation/invariants.py, the
 *   peak over every 1 s window of five tick intervals (six consecutive
 *   session ticks) whose ticks are all judged (fresh measurement, not silent,
 *   no emergency action, drive in OPERATION_ENABLED, |motor rpm| at or above
 *   the slowest running setpoint) of |g at the last tick - g at the first| /
 *   the time between them, at each radius. Only from the 5 Hz record. The g
 *   read are the columns, written to four decimals: the simulation, which
 *   computes them from the motor speed, may differ in the fourth decimal.
 * - Speed: the arm (`output_rpm`) and the motor (`measured_motor_rpm`), both
 *   measured, maximum of the absolute value.
 * - Verdicts: `rules_seen` of simulation/invariants.py, every safety rule
 *   that stood at some session tick, in the order first seen, with the most
 *   severe action it stood with (order of `SafetyAction`, types.py). From the
 *   telemetry, which has no rule, the `verdict` events stand in.
 * - End: the reason of the manifest (`end_reason`), and, for a session a
 *   safety verdict ended, the rule on the first `ending` or `finished` tick,
 *   or, if absent, on the immediately preceding session tick. Null when
 *   neither carries a rule or no ending tick was read; never an earlier rule
 *   (`EndReason.SAFETY_VERDICT`, raspberry-pi/src/training/runtime.py).
 *
 * Not in the summary yet, because nothing records it: the result of the
 * preflight (ANH-142). It will be a field of `summaryFiguresValidator`
 * computed here from what its ticket records; the report already says that
 * it is not available. The rider's feedback after the session (ANH-146) is
 * not a figure: it has its own table, `session_feedback`
 * (`convex/sessionFeedback.ts`), and is read beside the summary.
 */
import { v, type Infer } from "convex/values";
import { z } from "zod";

/** The files of a record the summary reads; the raw blocks are never read. */
export const SUMMARY_FILES = [
  "manifest.json",
  "ticks.csv",
  "events.jsonl",
] as const;

/** Largest manifest read, as `recordStorage` accepts it (2 MiB). */
export const MAX_MANIFEST_BYTES = 2 * 1024 * 1024;

/** A file is read by slices of this many bytes: memory stays bounded. */
export const READ_CHUNK_BYTES = 1024 * 1024;

/** A line longer than this is not a tick nor an event of the format: skipped. */
export const MAX_LINE_CHARS = 64 * 1024;

/** Rules kept in a summary, in the order first seen; more are counted only. */
export const MAX_VERDICTS = 32;

/** Longest text kept from a record (a rule, an action, an end reason). */
export const MAX_TEXT_CHARS = 120;

/** A g-rate window spans this many tick intervals (simulation/invariants.py). */
export const ARM_WINDOW_TICKS = 5;

/**
 * Slowest running setpoint, motor rpm, of a session without a programme:
 * `min_run_motor_rpm` of raspberry-pi/config/motion_limits.json as shipped.
 * A programme carries its own (`profile.min_run_rpm` of the manifest).
 */
export const DEFAULT_MIN_RUN_MOTOR_RPM = 55;

/** The actions that are not the control path's own doing (simulation/invariants.py). */
const EMERGENCY_ACTIONS = new Set(["QUICK_STOP", "GO_SILENT"]);

/** `SafetyAction` of raspberry-pi/src/training/types.py, least severe first. */
const ACTION_SEVERITY = [
  "NONE",
  "FREEZE",
  "REDUCE",
  "RAMP_DOWN",
  "QUICK_STOP",
  "GO_SILENT",
];

/** The runtime states of a session that is ending or over (`RuntimeState`). */
const ENDING_STATES = new Set(["ending", "finished"]);

/** The end reason whose verdict names a rule (`EndReason.SAFETY_VERDICT`). */
const SAFETY_VERDICT_END = "safety_verdict";

/** Where a summary comes from: the deposited record, or the 1 Hz telemetry. */
export const summarySourceValidator = v.union(
  v.literal("record"),
  v.literal("telemetry"),
);
export type SummarySource = Infer<typeof summarySourceValidator>;

const nullableNumber = v.union(v.number(), v.null());

/** Who counted the time in zone of a final summary (see the definitions). */
export const zoneSourceValidator = v.union(
  v.literal("console"),
  v.literal("ticks"),
);

/** The figures of one session (the definitions are at the top of this file). */
export const summaryFiguresValidator = v.object({
  /** Seconds, monotonic; null when no session tick was read. */
  durationS: nullableNumber,
  /** Session ticks (record) or points (telemetry) read. */
  samples: v.number(),
  heartRate: v.union(
    v.null(),
    v.object({
      minBpm: v.number(),
      meanBpm: v.number(),
      maxBpm: v.number(),
      /** Ticks or points that carried a confirmed rate. */
      samples: v.number(),
    }),
  ),
  /** Null without a programme (a manual session). */
  zone: v.union(
    v.null(),
    v.object({
      lowBpm: v.number(),
      highBpm: v.number(),
      inZoneS: v.number(),
      aboveZoneS: v.number(),
      belowZoneS: v.number(),
      /**
       * Final summary only: `console` when the three times are the
       * console's own counters, `ticks` when they were counted on the
       * ticks of the record. Absent from a provisional summary, and from a
       * final one computed before the console wrote its counters.
       */
      source: v.optional(zoneSourceValidator),
    }),
  ),
  maxOutputRpm: nullableNumber,
  maxMotorRpm: nullableNumber,
  reference: v.object({
    maxG: nullableNumber,
    maxGRate: nullableNumber,
    /** Metres, when the manifest carries the geometry (the simulation). */
    radiusM: nullableNumber,
  }),
  legTip: v.object({
    maxG: nullableNumber,
    maxGRate: nullableNumber,
    radiusM: nullableNumber,
    /** Whether that radius was measured; null when not known. */
    measured: v.union(v.boolean(), v.null()),
  }),
  verdicts: v.array(
    v.object({
      rule: v.string(),
      /** The most severe action seen with it; null when not known. */
      action: v.union(v.string(), v.null()),
      /** Seconds since the start when first seen; null when not known. */
      firstAtS: nullableNumber,
    }),
  ),
  /** Rules seen beyond `MAX_VERDICTS`, counted only. */
  verdictsOmitted: v.number(),
  endReason: v.union(v.string(), v.null()),
  endRule: v.union(v.string(), v.null()),
  /**
   * What the reader of the summary must know of its source, as codes:
   * `record_degraded` (the record says it misses data), `lines_skipped`
   * (lines of a file that were not of the format), `telemetry_truncated`
   * (more points than a summary reads), `events_truncated` (more events than
   * a summary reads), `ticks_out_of_order` (ticks or points ignored because
   * their time did not strictly increase).
   */
  warnings: v.array(v.string()),
});
export type SummaryFigures = Infer<typeof summaryFiguresValidator>;

/** The zone of a programme, both bounds in it. */
export type Zone = { lowBpm: number; highBpm: number };

/** The console's own counts of the time in, above and below the zone, seconds. */
export type ZoneCounters = { inS: number; aboveS: number; belowS: number };

/** What the summary needs of the manifest; every field may be missing. */
export type ManifestContext = {
  zone: Zone | null;
  /** Null unless the manifest carries all three counters, each finite and not negative. */
  zoneCounters: ZoneCounters | null;
  minRunMotorRpm: number;
  endReason: string | null;
  referenceRadiusM: number | null;
  legTipRadiusM: number | null;
  legTipMeasured: boolean | null;
};

/** Why a record could not be summarised: a code, never its content. */
export const recordProblemValidator = v.union(
  v.literal("missing_file"),
  v.literal("too_large"),
  v.literal("invalid_manifest"),
  v.literal("invalid_encoding"),
  v.literal("invalid_ticks"),
  v.literal("no_session_ticks"),
  v.literal("summary_failed"),
);
export type RecordProblem = Infer<typeof recordProblemValidator>;

/** A text of the record, cut to `MAX_TEXT_CHARS`. */
export function bounded(text: string): string {
  return text.length > MAX_TEXT_CHARS ? text.slice(0, MAX_TEXT_CHARS) : text;
}

function rounded(value: number, digits: number): number {
  const scale = 10 ** digits;
  return Math.round(value * scale) / scale;
}

function roundedOrNull(value: number | null, digits: number): number | null {
  return value === null ? null : rounded(value, digits);
}

function larger(current: number | null, value: number | null): number | null {
  if (value === null) return current;
  return current === null || value > current ? value : current;
}

function severity(action: string | null): number {
  return action === null ? -1 : ACTION_SEVERITY.indexOf(action);
}

// ---------------------------------------------------------------------------
// The manifest
// ---------------------------------------------------------------------------

const positive = z.number().positive();
const seconds = z.number().nonnegative();
const manifestShape = z.object({
  end_reason: z.string().nullish().catch(null),
  profile: z
    .object({
      zone_low_bpm: z.number(),
      zone_high_bpm: z.number(),
      min_run_rpm: z.number().nonnegative(),
    })
    .nullish()
    .catch(null),
  geometry: z
    .object({
      reference_radius_m: positive,
      leg_tip_radius_m: positive,
      leg_tip_measured: z.boolean(),
    })
    .nullish()
    .catch(null),
  end_observation: z
    .object({
      zone_in_s: seconds,
      zone_above_s: seconds,
      zone_below_s: seconds,
    })
    .nullish()
    .catch(null),
});

/**
 * What the summary reads of a manifest: the programme's zone and slowest
 * running setpoint, the console's zone counters, the end reason and the
 * geometry. A field the manifest does not carry, or carries in another
 * shape, is unknown, never guessed; null when the manifest is not an object
 * at all.
 */
export function manifestContext(value: unknown): ManifestContext | null {
  const parsed = manifestShape.safeParse(value);
  if (!parsed.success) return null;
  const { end_reason, profile, geometry, end_observation } = parsed.data;
  return {
    zone: profile
      ? { lowBpm: profile.zone_low_bpm, highBpm: profile.zone_high_bpm }
      : null,
    zoneCounters: end_observation
      ? {
          inS: end_observation.zone_in_s,
          aboveS: end_observation.zone_above_s,
          belowS: end_observation.zone_below_s,
        }
      : null,
    minRunMotorRpm: profile?.min_run_rpm ?? DEFAULT_MIN_RUN_MOTOR_RPM,
    endReason: end_reason ? bounded(end_reason) : null,
    referenceRadiusM: geometry?.reference_radius_m ?? null,
    legTipRadiusM: geometry?.leg_tip_radius_m ?? null,
    legTipMeasured: geometry?.leg_tip_measured ?? null,
  };
}

// ---------------------------------------------------------------------------
// Reading a file line by line
// ---------------------------------------------------------------------------

class InvalidEncodingError extends Error {}

/**
 * Call `onLine` with every complete line of `blob`, read by slices of
 * `chunkBytes`: the file is never held whole. As the record's reader does
 * (docs/enregistrement.md, "Fermeture, intégrité et lecture interrompue"),
 * a last line without its LF is not read. A line longer than `MAX_LINE_CHARS`
 * is skipped and counted. Throws on bytes that are not UTF-8.
 */
export async function forEachLine(
  blob: Blob,
  onLine: (line: string) => void,
  chunkBytes: number = READ_CHUNK_BYTES,
): Promise<{ overlong: number }> {
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let carry = "";
  let skipping = false;
  let overlong = 0;
  for (let offset = 0; offset < blob.size; offset += chunkBytes) {
    const bytes = await blob.slice(offset, offset + chunkBytes).arrayBuffer();
    const last = offset + chunkBytes >= blob.size;
    const chunk = new Uint8Array(bytes);
    let text: string;
    try {
      text = decoder.decode(chunk, { stream: !last });
    } catch (error) {
      if (error instanceof TypeError) throw new InvalidEncodingError();
      throw error;
    }
    let start = 0;
    for (
      let end = text.indexOf("\n", start);
      end !== -1;
      end = text.indexOf("\n", start)
    ) {
      const piece = text.slice(start, end);
      start = end + 1;
      if (skipping || carry.length + piece.length > MAX_LINE_CHARS) {
        overlong++;
        skipping = false;
        carry = "";
        continue;
      }
      const line = carry + piece;
      carry = "";
      onLine(line.endsWith("\r") ? line.slice(0, -1) : line);
    }
    if (!skipping) {
      carry += text.slice(start);
      if (carry.length > MAX_LINE_CHARS) {
        carry = "";
        skipping = true;
      }
    }
  }
  return { overlong };
}

/**
 * The fields of one CSV line as Python's `csv` module writes them: a field
 * holding a comma or a quote is quoted, and a quote inside it doubled.
 */
export function csvFields(line: string): string[] {
  const fields: string[] = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const character = line[i];
    if (quoted) {
      if (character !== '"') field += character;
      else if (line[i + 1] === '"') {
        field += '"';
        i++;
      } else quoted = false;
    } else if (character === '"') quoted = true;
    else if (character === ",") {
      fields.push(field);
      field = "";
    } else field += character;
  }
  fields.push(field);
  return fields;
}

// ---------------------------------------------------------------------------
// The figures
// ---------------------------------------------------------------------------

/** One session tick, or one telemetry point, as the figures read it. */
export type Sample = {
  /** Seconds since the start command. */
  t: number;
  bpm: number | null;
  outputRpm: number | null;
  motorRpm: number | null;
  gReference: number | null;
  gLegTip: number | null;
  rule: string | null;
  action: string | null;
  state: string | null;
  /** Whether this tick may count in a g-rate window (see the definitions). */
  judged: boolean;
};

type Verdict = { action: string | null; firstAtS: number | null };

/**
 * The figures of a session, built one sample at a time, in the order of the
 * record. `rates` says whether the samples are ticks of the 5 Hz record, the
 * only ones a variation of g is judged on.
 */
export class SummaryBuilder {
  private previousT: number | null = null;
  private samples = 0;
  private lastT: number | null = null;
  private hr = { min: Infinity, max: -Infinity, sum: 0, count: 0 };
  private zoneTime = { in: 0, above: 0, below: 0 };
  private maxOutputRpm: number | null = null;
  private maxMotorRpm: number | null = null;
  private maxGReference: number | null = null;
  private maxGLegTip: number | null = null;
  private maxRateReference: number | null = null;
  private maxRateLegTip: number | null = null;
  private window: Sample[] = [];
  private verdicts = new Map<string, Verdict>();
  private omitted = new Set<string>();
  private previousRule: string | null = null;
  private outOfOrder = 0;
  private endingRule: string | null | undefined = undefined;

  constructor(
    private readonly zone: Zone | null,
    private readonly rates: boolean,
  ) {}

  add(sample: Sample): void {
    const previous = this.previousT;
    if (previous !== null && sample.t <= previous) {
      this.outOfOrder++;
      return;
    }
    this.previousT = sample.t;
    if (!(sample.t > 0)) return;
    this.samples++;
    this.lastT = sample.t;
    if (sample.bpm !== null) {
      this.hr.min = Math.min(this.hr.min, sample.bpm);
      this.hr.max = Math.max(this.hr.max, sample.bpm);
      this.hr.sum += sample.bpm;
      this.hr.count++;
      if (this.zone !== null && previous !== null) {
        const interval = sample.t - previous;
        if (sample.bpm < this.zone.lowBpm) this.zoneTime.below += interval;
        else if (sample.bpm > this.zone.highBpm)
          this.zoneTime.above += interval;
        else this.zoneTime.in += interval;
      }
    }
    this.maxOutputRpm = larger(
      this.maxOutputRpm,
      sample.outputRpm === null ? null : Math.abs(sample.outputRpm),
    );
    this.maxMotorRpm = larger(
      this.maxMotorRpm,
      sample.motorRpm === null ? null : Math.abs(sample.motorRpm),
    );
    this.maxGReference = larger(this.maxGReference, sample.gReference);
    this.maxGLegTip = larger(this.maxGLegTip, sample.gLegTip);
    if (sample.rule !== null)
      this.verdict(sample.rule, sample.action, sample.t);
    if (
      this.endingRule === undefined &&
      sample.state !== null &&
      ENDING_STATES.has(sample.state)
    )
      this.endingRule = sample.rule ?? this.previousRule;
    this.previousRule = sample.rule;
    if (this.rates) this.judge(sample);
  }

  /** A verdict seen at `atS` seconds (null when its time is not known). */
  verdict(rule: string, action: string | null, atS: number | null): void {
    const name = bounded(rule);
    const known = this.verdicts.get(name);
    if (known === undefined) {
      if (this.verdicts.size >= MAX_VERDICTS) {
        this.omitted.add(name);
        return;
      }
      this.verdicts.set(name, {
        action: action === null ? null : bounded(action),
        firstAtS: atS === null ? null : rounded(atS, 1),
      });
    } else if (action !== null && severity(action) > severity(known.action)) {
      known.action = bounded(action);
    }
  }

  /** The g-rate window ending on this tick, when all its ticks are judged. */
  private judge(sample: Sample): void {
    this.window.push(sample);
    if (this.window.length > ARM_WINDOW_TICKS + 1) this.window.shift();
    if (this.window.length <= ARM_WINDOW_TICKS) return;
    if (!this.window.every((tick) => tick.judged)) return;
    const first = this.window[0];
    const dt = sample.t - first.t;
    if (!(dt > 0)) return;
    const rate = (a: number | null, b: number | null) =>
      a === null || b === null ? null : Math.abs(b - a) / dt;
    this.maxRateReference = larger(
      this.maxRateReference,
      rate(first.gReference, sample.gReference),
    );
    this.maxRateLegTip = larger(
      this.maxRateLegTip,
      rate(first.gLegTip, sample.gLegTip),
    );
  }

  figures(context: {
    endReason: string | null;
    referenceRadiusM: number | null;
    legTipRadiusM: number | null;
    legTipMeasured: boolean | null;
    warnings: string[];
  }): SummaryFigures {
    const endRule =
      context.endReason === SAFETY_VERDICT_END
        ? (this.endingRule ?? null)
        : null;
    return {
      durationS: roundedOrNull(this.lastT, 1),
      samples: this.samples,
      heartRate:
        this.hr.count === 0
          ? null
          : {
              minBpm: this.hr.min,
              meanBpm: rounded(this.hr.sum / this.hr.count, 1),
              maxBpm: this.hr.max,
              samples: this.hr.count,
            },
      zone:
        this.zone === null
          ? null
          : {
              lowBpm: this.zone.lowBpm,
              highBpm: this.zone.highBpm,
              inZoneS: rounded(this.zoneTime.in, 1),
              aboveZoneS: rounded(this.zoneTime.above, 1),
              belowZoneS: rounded(this.zoneTime.below, 1),
            },
      maxOutputRpm: roundedOrNull(this.maxOutputRpm, 2),
      maxMotorRpm: roundedOrNull(this.maxMotorRpm, 0),
      reference: {
        maxG: roundedOrNull(this.maxGReference, 4),
        maxGRate: this.rates ? roundedOrNull(this.maxRateReference, 4) : null,
        radiusM: context.referenceRadiusM,
      },
      legTip: {
        maxG: roundedOrNull(this.maxGLegTip, 4),
        maxGRate: this.rates ? roundedOrNull(this.maxRateLegTip, 4) : null,
        radiusM: context.legTipRadiusM,
        measured: context.legTipMeasured,
      },
      verdicts: [...this.verdicts].map(([rule, verdict]) => ({
        rule,
        ...verdict,
      })),
      verdictsOmitted: this.omitted.size,
      endReason: context.endReason,
      endRule,
      warnings:
        this.outOfOrder > 0
          ? [...context.warnings, "ticks_out_of_order"]
          : context.warnings,
    };
  }
}

/**
 * The zone of a final summary: the console's own counters when the manifest
 * carries them, otherwise the times counted on the ticks, each saying which.
 * Null, whatever the counters, without a programme's zone.
 */
export function finalZone(
  zone: SummaryFigures["zone"],
  counters: ZoneCounters | null,
): SummaryFigures["zone"] {
  if (zone === null) return null;
  if (counters === null) return { ...zone, source: "ticks" };
  return {
    lowBpm: zone.lowBpm,
    highBpm: zone.highBpm,
    inZoneS: rounded(counters.inS, 1),
    aboveZoneS: rounded(counters.aboveS, 1),
    belowZoneS: rounded(counters.belowS, 1),
    source: "console",
  };
}

// ---------------------------------------------------------------------------
// ticks.csv
// ---------------------------------------------------------------------------

/** The columns of `ticks.csv` the summary reads, found by name in the header. */
const TICK_COLUMNS = [
  "t",
  "state",
  "drive_state",
  "measured_motor_rpm",
  "measured_fresh",
  "output_rpm",
  "g_reference",
  "g_leg_tip",
  "safety_action",
  "safety_rule",
  "silent",
  "hr_confirmed",
] as const;
type TickColumn = (typeof TICK_COLUMNS)[number];

/** A number of the CSV, or null for the empty field; NaN when unreadable. */
function csvNumber(field: string | undefined): number | null {
  if (field === undefined || field === "") return null;
  const value = Number(field);
  return Number.isFinite(value) ? value : NaN;
}

/**
 * The ticks of `ticks.csv`, read line by line into a `SummaryBuilder`. The
 * first line is the header: the columns are found by name, so a column added
 * to the format moves nothing. A line that is not a tick (wrong number of
 * fields, a number that is not one) is skipped and counted.
 */
export class TickReader {
  private columns: Record<TickColumn, number> | null = null;
  private width = 0;
  skipped = 0;
  invalidHeader = false;

  constructor(
    private readonly builder: SummaryBuilder,
    private readonly minRunMotorRpm: number,
  ) {}

  /** Whether a header naming every column read was found. */
  get ready(): boolean {
    return this.columns !== null;
  }

  line(text: string): void {
    const fields = csvFields(text);
    if (this.columns === null) {
      if (this.invalidHeader) return;
      const found: Partial<Record<TickColumn, number>> = {};
      for (const column of TICK_COLUMNS) {
        const index = fields.indexOf(column);
        if (index === -1) {
          this.invalidHeader = true;
          return;
        }
        found[column] = index;
      }
      this.columns = found as Record<TickColumn, number>;
      this.width = fields.length;
      return;
    }
    if (fields.length !== this.width) {
      this.skipped++;
      return;
    }
    const at = (column: TickColumn) => fields[this.columns![column]];
    const t = csvNumber(at("t"));
    const numbers = {
      bpm: csvNumber(at("hr_confirmed")),
      outputRpm: csvNumber(at("output_rpm")),
      motorRpm: csvNumber(at("measured_motor_rpm")),
      gReference: csvNumber(at("g_reference")),
      gLegTip: csvNumber(at("g_leg_tip")),
    };
    if (
      t === null ||
      Number.isNaN(t) ||
      Object.values(numbers).some(Number.isNaN)
    ) {
      this.skipped++;
      return;
    }
    const action = at("safety_action") || null;
    const motor = numbers.motorRpm;
    this.builder.add({
      t,
      ...numbers,
      rule: at("safety_rule") || null,
      action,
      state: at("state") || null,
      judged:
        at("measured_fresh") === "True" &&
        at("silent") === "False" &&
        action !== null &&
        !EMERGENCY_ACTIONS.has(action) &&
        at("drive_state") === "OPERATION_ENABLED" &&
        motor !== null &&
        Math.abs(motor) >= this.minRunMotorRpm,
    });
  }
}

// ---------------------------------------------------------------------------
// events.jsonl
// ---------------------------------------------------------------------------

/** The prefix of the warning that says what a record misses (docs/enregistrement.md). */
export const RECORD_DEGRADED_PREFIX = "record_degraded:";

/** What the summary reads of `events.jsonl`: whether the record says it misses data. */
export class EventReader {
  degraded = false;
  skipped = 0;

  line(text: string): void {
    let event: unknown;
    try {
      event = JSON.parse(text);
    } catch {
      this.skipped++;
      return;
    }
    if (
      typeof event === "object" &&
      event !== null &&
      "kind" in event &&
      "detail" in event &&
      event.kind === "warning" &&
      typeof event.detail === "string" &&
      event.detail.startsWith(RECORD_DEGRADED_PREFIX)
    )
      this.degraded = true;
  }
}

// ---------------------------------------------------------------------------
// A record
// ---------------------------------------------------------------------------

/**
 * The figures of a deposited record, opening its files one at a time (null
 * when missing from storage), or why they cannot be computed. Bounded: the
 * manifest is read whole (2 MiB at most), the two streams line by line, and
 * nothing else of the record is opened, however many raw blocks it holds.
 */
export async function summarizeRecordFiles(files: {
  manifest: () => Promise<Blob | null>;
  ticks: () => Promise<Blob | null>;
  events: () => Promise<Blob | null>;
  maxFileBytes: number;
}): Promise<{ figures: SummaryFigures } | { problem: RecordProblem }> {
  let context: ManifestContext | null;
  {
    const manifest = await files.manifest();
    if (manifest === null) return { problem: "missing_file" };
    if (manifest.size > MAX_MANIFEST_BYTES) return { problem: "too_large" };
    const bytes = await manifest.arrayBuffer();
    let value: unknown;
    try {
      const text = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      value = JSON.parse(text);
    } catch (error) {
      if (error instanceof TypeError || error instanceof SyntaxError)
        return { problem: "invalid_manifest" };
      throw error;
    }
    context = manifestContext(value);
  }
  if (context === null) return { problem: "invalid_manifest" };
  const builder = new SummaryBuilder(context.zone, true);
  const tickReader = new TickReader(builder, context.minRunMotorRpm);
  const eventReader = new EventReader();
  let overlong = 0;
  try {
    {
      const ticks = await files.ticks();
      if (ticks === null) return { problem: "missing_file" };
      if (ticks.size > files.maxFileBytes) return { problem: "too_large" };
      overlong += (await forEachLine(ticks, (line) => tickReader.line(line)))
        .overlong;
    }
    {
      const events = await files.events();
      if (events === null) return { problem: "missing_file" };
      if (events.size > files.maxFileBytes) return { problem: "too_large" };
      overlong += (await forEachLine(events, (line) => eventReader.line(line)))
        .overlong;
    }
  } catch (error) {
    if (error instanceof InvalidEncodingError)
      return { problem: "invalid_encoding" };
    throw error;
  }
  if (!tickReader.ready) return { problem: "invalid_ticks" };
  const warnings: string[] = [];
  if (eventReader.degraded) warnings.push("record_degraded");
  if (overlong + tickReader.skipped + eventReader.skipped > 0)
    warnings.push("lines_skipped");
  const figures = builder.figures({
    endReason: context.endReason,
    referenceRadiusM: context.referenceRadiusM,
    legTipRadiusM: context.legTipRadiusM,
    legTipMeasured: context.legTipMeasured,
    warnings,
  });
  if (figures.samples === 0) return { problem: "no_session_ticks" };
  return {
    figures: {
      ...figures,
      zone: finalZone(figures.zone, context.zoneCounters),
    },
  };
}

/**
 * The rule and the action of a `verdict` event. The console writes
 * `rule (ACTION): detail` (raspberry-pi/src/record/session.py,
 * `_note_changes`), the simulation `rule` alone (simulation/harness.py);
 * `cleared` is the end of a verdict, not one. Null for anything else.
 */
export function parseVerdict(
  detail: string,
): { rule: string; action: string | null } | null {
  if (detail === "cleared") return null;
  const match = /^([A-Za-z0-9_.-]+)(?: \(([A-Z_]+)\))?(?::\s|$)/.exec(detail);
  if (match === null) return null;
  return { rule: match[1], action: match[2] ?? null };
}
