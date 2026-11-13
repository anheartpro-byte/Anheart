/**
 * The programme catalogue (ANH-137): its fields and its validation.
 *
 * A programme is a Pi `TrainingProfile` (`raspberry-pi/src/training/plan.py`)
 * with its fields in camelCase. `programmeRefusal` mirrors `parse_profile` and
 * the checks of `TrainingProfile` line for line: same verdict, same order,
 * same message. `contracts/programme-validation-cases.json` holds the cases
 * both sides are tested against (`convex/programmes.test.ts` and
 * `raspberry-pi/tests/test_programme_validation_parity.py`).
 *
 * The limits plan.py takes from the shipped medical file (minimum recovery and
 * cool-down, zone ceiling fraction) are read from that same file here, so the
 * two sides cannot disagree; a programme records the version of that file as
 * `medicalParametersVersion`.
 */
import { v, type ObjectType } from "convex/values";
import shippedMedical from "../../raspberry-pi/config/medical_parameters.json";

/** `NAMEPLATE_MOTOR_RPM`: `max_rpm` above it needs `allow_above_nameplate`. */
export const NAMEPLATE_MOTOR_RPM = 1380;
/** `COMMISSIONED_DECEL_S`: the drive's measured ramp, a floor under the cool-down. */
const COMMISSIONED_DECEL_S = 4.0;
const MIN_RECOVERY_S = shippedMedical.parameters.recovery_min_s;
const MIN_COOLDOWN_S = shippedMedical.parameters.cooldown_min_s;
export const ZONE_CEILING_FRACTION = shippedMedical.parameters.zone_ceiling_fraction;
/** The version of the medical file whose limits `programmeRefusal` applies. */
export const PROGRAMME_MEDICAL_VERSION = shippedMedical.version;
const SUBJECT_HR_MAX_MIN = 100;
const SUBJECT_HR_MAX_MAX = 220;
/** `PROFILE_ID_PATTERN`; without the `m` flag `$` is the end of the text only. */
const PROFILE_ID_PATTERN = /^[a-z][a-z0-9_]{0,63}$/;
/** What Python's `str.strip()` removes (`str.isspace`), which is not what `trim()` removes. */
const PYTHON_WHITESPACE = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009,
  0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
]);
const blank = (text: string) =>
  Array.from(text).every((character) => PYTHON_WHITESPACE.has(character.charCodeAt(0)));

/** Version of the shape of a programme row. */
export const PROGRAMME_SCHEMA_VERSION = 1;

/** `Channel`: a BITalino input, by the wire name the Pi uses. */
export const programmeChannelValidator = v.union(
  v.literal("ECG"),
  v.literal("EDA"),
  v.literal("SpO2"),
  v.literal("RESP"),
  v.literal("EMG"),
  v.literal("LUX"),
);

/** The fields of a `TrainingProfile`, in camelCase. */
export const programmeProfileFields = {
  profileId: v.string(),
  name: v.string(),
  totalDurationS: v.number(),
  baselineS: v.number(),
  warmupMaxS: v.number(),
  holdMinS: v.number(),
  cooldownS: v.number(),
  recoveryS: v.number(),
  zoneLowBpm: v.number(),
  zoneHighBpm: v.number(),
  hardMaxBpm: v.number(),
  criticalBpm: v.number(),
  subjectHrMax: v.number(),
  minRunRpm: v.number(),
  maxRpm: v.number(),
  warmupRpmCeilingFraction: v.number(),
  channels: v.array(programmeChannelValidator),
  allowAboveNameplate: v.boolean(),
};
export type ProgrammeProfile = ObjectType<typeof programmeProfileFields>;

/** Draft, then validated by a second admin, then published, then retired. */
export const programmeStatusValidator = v.union(
  v.literal("draft"),
  v.literal("validated"),
  v.literal("published"),
  v.literal("retired"),
);

/** A row of `programmes`, and the copy of it `programme_revisions` keeps. */
export const programmeFields = {
  ...programmeProfileFields,
  // null: the Anheart catalogue, the only case written for now.
  organizationId: v.union(v.id("organizations"), v.null()),
  status: programmeStatusValidator,
  schemaVersion: v.number(),
  revision: v.number(), // 0 until the first publication, +1 at each
  medicalParametersVersion: v.string(),
  medicalSignoff: v.union(
    v.object({ by: v.string(), at: v.number(), note: v.string() }),
    v.null(),
  ),
  // The report in Convex Storage: uploaded by an admin today, deposited by a
  // simulation run started from the site later (docs/programmes.md).
  simulationReport: v.union(v.id("_storage"), v.null()),
  validatedBy: v.union(v.id("users"), v.null()),
  createdBy: v.id("users"),
  updatedBy: v.id("users"),
  createdAt: v.number(),
  updatedAt: v.number(),
};

/** `Violation` of plan.py, in the order `_CHECKS` reports them. */
export type ProgrammeViolation =
  | "bad_profile_id"
  | "empty_name"
  | "non_positive_duration"
  | "cooldown_faster_than_drive_ramp"
  | "cooldown_too_short"
  | "recovery_too_short"
  | "hold_too_short"
  | "bpm_not_positive"
  | "subject_hr_max_implausible"
  | "zone_not_ascending"
  | "zone_above_subject_ceiling"
  | "hard_max_above_subject_max"
  | "critical_not_above_hard_max"
  | "rpm_not_positive"
  | "min_run_above_max"
  | "rpm_above_nameplate"
  | "warmup_fraction_out_of_range"
  | "warmup_ceiling_below_min_run"
  | "ecg_channel_missing"
  | "duplicate_channel";

/** `Malformed` (a whole number that is not one) or `Rejected`, with plan.py's `detail`. */
export type ProgrammeRefusal = {
  verdict: "malformed" | "rejected";
  violations: ProgrammeViolation[];
  detail: string;
};

const positiveFinite = (value: number) => Number.isFinite(value) && value > 0;

/** The fields `_Reader.integer` reads, by their name in the Pi's document. */
const WHOLE_FIELDS = [
  ["zoneLowBpm", "zone_low_bpm"],
  ["zoneHighBpm", "zone_high_bpm"],
  ["hardMaxBpm", "hard_max_bpm"],
  ["criticalBpm", "critical_bpm"],
  ["subjectHrMax", "subject_hr_max"],
  ["minRunRpm", "min_run_rpm"],
  ["maxRpm", "max_rpm"],
] as const;

/** `_CHECKS`: each violation with what passes it, in plan.py's order. */
const CHECKS: ReadonlyArray<
  readonly [ProgrammeViolation, (p: ProgrammeProfile) => boolean]
> = [
  ["bad_profile_id", (p) => PROFILE_ID_PATTERN.test(p.profileId)],
  ["empty_name", (p) => !blank(p.name)],
  [
    "non_positive_duration",
    (p) =>
      [
        p.totalDurationS,
        p.baselineS,
        p.warmupMaxS,
        p.holdMinS,
        p.cooldownS,
        p.recoveryS,
      ].every(positiveFinite),
  ],
  ["cooldown_faster_than_drive_ramp", (p) => p.cooldownS >= COMMISSIONED_DECEL_S],
  ["cooldown_too_short", (p) => p.cooldownS >= MIN_COOLDOWN_S],
  ["recovery_too_short", (p) => p.recoveryS >= MIN_RECOVERY_S],
  // `hold_s`: the remainder, summed in plan.py's order so the floats agree.
  [
    "hold_too_short",
    (p) =>
      p.totalDurationS -
        (p.baselineS + p.warmupMaxS + p.cooldownS + p.recoveryS) >=
      p.holdMinS,
  ],
  [
    "bpm_not_positive",
    (p) =>
      [p.zoneLowBpm, p.zoneHighBpm, p.hardMaxBpm, p.criticalBpm].every(
        (bpm) => bpm > 0,
      ),
  ],
  [
    "subject_hr_max_implausible",
    (p) =>
      SUBJECT_HR_MAX_MIN <= p.subjectHrMax && p.subjectHrMax <= SUBJECT_HR_MAX_MAX,
  ],
  ["zone_not_ascending", (p) => p.zoneLowBpm < p.zoneHighBpm],
  [
    "zone_above_subject_ceiling",
    (p) => p.zoneHighBpm <= ZONE_CEILING_FRACTION * p.subjectHrMax,
  ],
  ["hard_max_above_subject_max", (p) => p.hardMaxBpm <= p.subjectHrMax],
  ["critical_not_above_hard_max", (p) => p.criticalBpm > p.hardMaxBpm],
  ["rpm_not_positive", (p) => p.minRunRpm > 0 && p.maxRpm > 0],
  ["min_run_above_max", (p) => p.minRunRpm <= p.maxRpm],
  [
    "rpm_above_nameplate",
    (p) => p.allowAboveNameplate || p.maxRpm <= NAMEPLATE_MOTOR_RPM,
  ],
  [
    "warmup_fraction_out_of_range",
    (p) =>
      positiveFinite(p.warmupRpmCeilingFraction) &&
      p.warmupRpmCeilingFraction <= 1,
  ],
  // Skipped when the two fields it derives from are themselves refused.
  [
    "warmup_ceiling_below_min_run",
    (p) =>
      !positiveFinite(p.warmupRpmCeilingFraction) ||
      p.maxRpm <= 0 ||
      !Number.isFinite(p.maxRpm * p.warmupRpmCeilingFraction) ||
      Math.floor(p.maxRpm * p.warmupRpmCeilingFraction) >= p.minRunRpm,
  ],
  ["ecg_channel_missing", (p) => p.channels.includes("ECG")],
  ["duplicate_channel", (p) => new Set(p.channels).size === p.channels.length],
];

/** Why plan.py would not build this profile, or null when it would. */
export function programmeRefusal(p: ProgrammeProfile): ProgrammeRefusal | null {
  // From 1e21 on, JSON.stringify writes a whole number as `1e+21`, which the
  // Pi reads as a float: such a number is malformed here too.
  const problems = WHOLE_FIELDS.filter(
    ([key]) => !Number.isInteger(p[key]) || Math.abs(p[key]) >= 1e21,
  ).map(([, wire]) => `profile: '${wire}' must be a whole number, got float`);
  if (problems.length > 0)
    return { verdict: "malformed", violations: [], detail: problems.join("; ") };
  const violations = CHECKS.filter(([, passes]) => !passes(p)).map(
    ([violation]) => violation,
  );
  if (violations.length === 0) return null;
  return {
    verdict: "rejected",
    violations,
    detail: `training profile refused: ${violations.join(", ")}`,
  };
}
