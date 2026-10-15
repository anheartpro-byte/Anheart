/**
 * Frontend helpers for centrifuge training sessions.
 *
 * The Pi is the authority on every value here; these helpers only format what
 * it reports and mirror the checks convex/training.ts runs so the dashboard can
 * warn before the server refuses.
 *
 * convex/training.ts imports LIVE_FRESH_MS from this file: keep it free of
 * browser-only code and of "@/" imports, which the Convex bundle cannot resolve.
 */

import {
  estimateHrMax,
  zoneCeilingBpm,
  type MedicalParameters,
} from "./medicalParameters";

/** Motor shaft turns per arm turn (the gearbox). Arm rpm = motor rpm / ratio. */
export const GEAR_RATIO = 49.79;

const HR_MAX_MIN = 100;
const HR_MAX_MAX = 220;

export type TrainingKind = "auto" | "manual" | "recording";
export type TrainingOrigin = "remote" | "local";

export type TrainingProfile = {
  profileId: string;
  name: string;
  totalDurationS: number;
  zoneLowBpm: number;
  zoneHighBpm: number;
  hardMaxBpm: number;
  criticalBpm: number;
  subjectHrMax: number;
  minRunRpm: number;
  maxRpm: number;
};

export type LiveState = {
  runMode: string;
  phase: string;
  bpm?: number;
  motorRpm: number;
  outputRpm: number;
  setpointMotorRpm: number;
  gLoad: number;
  safetyAction: string;
  driveState?: string;
  sessionId?: string;
  updatedAt: number;
};

export type TelemetryPoint = {
  t: number;
  elapsedS: number;
  phase: string;
  bpm?: number;
  motorRpm: number;
  outputRpm: number;
  setpointMotorRpm: number;
  gLoad: number;
  safetyAction: string;
};

/**
 * A machine state older than this means the machine cannot be relied on to
 * answer (it reports every 10 s). One definition for the server
 * (convex/training.ts, convex/machines.ts) and for the dashboard
 * (hooks/use-freshness.ts).
 */
export const LIVE_FRESH_MS = 90_000;

/**
 * The last sign of life of an active session older than this no longer stands
 * for "now" (the machine sends its points every 5 s).
 */
export const TELEMETRY_FRESH_MS = 20_000;

/**
 * An active session of which the server has had no news for this long is no
 * longer shown as running: the server's job marks it "interrupted, end
 * unknown" (convex/training.ts, `interruptSilentSessions`). News of a session
 * is a heartbeat of its machine that names it as the session it runs, a point
 * or an event of it received, or its registration. A machine that runs it
 * says so with every heartbeat (every 10 s), so ten minutes without a word of
 * it is a console stopped, a machine switched off, or a link down that long.
 * The mark is undone by the next heartbeat that names it.
 */
export const SESSION_SILENCE_MS = 10 * 60_000;

/**
 * How far after `now` a date may be and still be believed. Two dates written
 * by one clock differ by milliseconds; a date further ahead was not written by
 * that clock, and nothing can be said of its age.
 */
export const FUTURE_TOLERANCE_MS = 5_000;

/**
 * Whether a datum stamped `updatedAt` is still fresh at `now`, both read on
 * the same clock. Without a timestamp it never is: absence is no reason to
 * show a value as current. Nor is a date from the future beyond the tolerance.
 */
export function isFresh(
  updatedAt: number | null | undefined,
  now: number,
  freshMs: number = LIVE_FRESH_MS,
): boolean {
  if (typeof updatedAt !== "number") return false;
  const age = now - updatedAt;
  return age < freshMs && age >= -FUTURE_TOLERANCE_MS;
}

/**
 * Whether a telemetry point is dated after the server received it, beyond the
 * tolerance. No point is measured after its own reception: such a date comes
 * from a machine whose clock is ahead, and tells nothing of the point's age.
 * Unlike an age, this never changes as time passes: a point dated so does not
 * become believable a few seconds later.
 */
export function datedAfterReception(
  measuredAt: number,
  receivedAt: number | null | undefined,
): boolean {
  return (
    typeof receivedAt === "number" &&
    measuredAt - receivedAt > FUTURE_TOLERANCE_MS
  );
}

/**
 * The status to show for a machine. Its record says "online" or "in_session"
 * until the server's job notices the silence, up to a minute after the signal
 * stopped being fresh: a machine whose last signal is not fresh is shown
 * offline, whatever its record still says.
 */
export function shownMachineStatus(
  status: string,
  signalFresh: boolean,
): string {
  return signalFresh ? status : "offline";
}

export function armRpm(motorRpm: number): number {
  return motorRpm / GEAR_RATIO;
}

export function isTrainingKind(kind: string | undefined): boolean {
  return kind === "auto" || kind === "manual";
}

/** "12:05" or "1:02:05" from a number of seconds (negative clamps to 0). */
export function formatClock(totalSeconds: number): string {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = h > 0 ? String(m).padStart(2, "0") : String(m);
  return `${h > 0 ? `${h}:` : ""}${mm}:${String(sec).padStart(2, "0")}`;
}

/** "30 min" / "1 h 05" style duration for programme lengths. */
export function formatMinutes(totalSeconds: number): string {
  const minutes = Math.round(totalSeconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const h = Math.floor(minutes / 60);
  return `${h} h ${String(minutes % 60).padStart(2, "0")}`;
}

/**
 * Whether a max heart rate or a birth year is one the server uses: a finite
 * whole number, as convex/training.ts requires of both. Any other value on
 * record counts as not set.
 */
export function isWholeNumber(value: unknown): value is number {
  return Number.isInteger(value);
}

/**
 * Measured maximum if valid, else the estimate from the current signed
 * medical parameters. A missing reference or unusable measure gives nothing.
 */
export function effectiveHrMax(
  hrMax: number | undefined | null,
  birthYear: number | undefined | null,
  now: number,
  medicalParameters: MedicalParameters | null,
): { value: number; source: "measured" | "estimated" } | null {
  if (hrMax !== undefined && hrMax !== null) {
    return isWholeNumber(hrMax) && hrMax >= HR_MAX_MIN && hrMax <= HR_MAX_MAX
      ? { value: hrMax, source: "measured" }
      : null;
  }
  if (medicalParameters === null) return null;
  if (!isWholeNumber(birthYear)) return null;
  const age = new Date(now).getUTCFullYear() - birthYear;
  const value = estimateHrMax(age, medicalParameters.hrMaxFormula);
  return value === null ? null : { value, source: "estimated" };
}

export function zoneCeiling(
  hrMax: number,
  medicalParameters: MedicalParameters,
): number {
  return zoneCeilingBpm(medicalParameters.zoneCeilingFraction, hrMax);
}

/**
 * Read an optional numeric field the backend may or may not return yet
 * (e.g. `hrMax` on `users.getUserById`). Undefined when absent.
 */
export function readOptionalNumber(
  obj: object,
  key: string,
): number | undefined {
  if (!(key in obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  return typeof value === "number" ? value : undefined;
}

export function readOptionalString(
  obj: object,
  key: string,
): string | undefined {
  if (!(key in obj)) return undefined;
  const value = (obj as Record<string, unknown>)[key];
  return typeof value === "string" ? value : undefined;
}
