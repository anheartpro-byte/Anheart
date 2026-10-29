import { readFileSync } from "node:fs";
import path from "node:path";
import type { RunInfo } from "./run";

export type Medical = {
  hr_hard_max_bpm: number;
  hr_critical_bpm: number;
  min_rider_age: number;
  motion_limits: { min_run_motor_rpm: number };
  recovery_min_s: number;
  cooldown_min_s: number;
};

/** The signed medical file the console reads (Convex holds the same version). */
export function medicalOf(run: RunInfo): Medical {
  const file = path.join(run.repoRoot, "raspberry-pi", "config", "medical_parameters.json");
  return (JSON.parse(readFileSync(file, "utf8")) as { parameters: Medical }).parameters;
}

/**
 * A programme short enough to watch to its end: the console's shipped
 * `standard_30_min` (its zone, reference subject and ceilings), with the tiers
 * and limits of the medical file. Only the durations are a test choice.
 */
export function shortProfile(standard: Record<string, unknown>, medical: Medical) {
  // Test choice: 10 s at rest, up to 60 s of warm-up, 30 s of hold, 20 s of
  // cool-down, and the shortest recovery the medical file allows.
  const baseline_s = 10;
  const warmup_max_s = 60;
  const hold_min_s = 30;
  const cooldown_s = Math.max(20, medical.cooldown_min_s);
  const recovery_s = medical.recovery_min_s;
  return {
    profile_id: "e2e_court",
    name: "E2E court",
    total_duration_s: baseline_s + warmup_max_s + hold_min_s + cooldown_s + recovery_s,
    baseline_s,
    warmup_max_s,
    hold_min_s,
    cooldown_s,
    recovery_s,
    zone_low_bpm: standard.zone_low_bpm,
    zone_high_bpm: standard.zone_high_bpm,
    hard_max_bpm: medical.hr_hard_max_bpm,
    critical_bpm: medical.hr_critical_bpm,
    subject_hr_max: standard.subject_hr_max,
    min_run_rpm: medical.motion_limits.min_run_motor_rpm,
    max_rpm: standard.max_rpm,
    warmup_rpm_ceiling_fraction: standard.warmup_rpm_ceiling_fraction,
    channels: standard.channels,
    allow_above_nameplate: standard.allow_above_nameplate,
  };
}

/** The name the short programme is listed under, on the console and on the site. */
export const PROFILE_NAME = "E2E court";
