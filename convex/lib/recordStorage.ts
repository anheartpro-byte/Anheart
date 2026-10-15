import { v } from "convex/values";
import { z } from "zod";

export const MAX_RECORD_FILE_BYTES = 16 * 1024 * 1024;
export const MAX_RECORD_FILES = 40_000;
export const MANIFEST_PAGE_SIZE = 1024;
export const MAX_MANIFEST_PAGES = 128;
export function recordBucket(path: string): number {
  let hash = 2166136261;
  for (const character of path)
    hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0;
  return hash % 64;
}
export const RECORD_MEDIA_PREFIX = "application/x-anheart-record;nonce=";
export const recordState = v.union(
  v.literal("none"),
  v.literal("partial"),
  v.literal("complete"),
  v.literal("rejected"),
);
export const recordPath = z
  .string()
  .max(160)
  .regex(/^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$/);
export const uploadRequest = z
  .object({
    sessionId: z.string().min(1),
    path: recordPath,
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    size: z.number().int().min(0).max(MAX_RECORD_FILE_BYTES),
  })
  .refine((file) => file.path !== "manifest.json" || file.size <= 2 * 1024 * 1024);
export const commitRequest = uploadRequest.safeExtend({
  storageId: z.string().min(1),
});
const recordProfile = z.strictObject({
  total_duration_s: z.number(),
  baseline_s: z.number(),
  warmup_max_s: z.number(),
  hold_min_s: z.number(),
  cooldown_s: z.number(),
  recovery_s: z.number(),
  zone_low_bpm: z.number(),
  zone_high_bpm: z.number(),
  hard_max_bpm: z.number(),
  critical_bpm: z.number(),
  subject_hr_max: z.number(),
  min_run_rpm: z.number(),
  max_rpm: z.number(),
  warmup_rpm_ceiling_fraction: z.number(),
  channels: z.array(z.string()),
  allow_above_nameplate: z.boolean(),
  source_rev: z.number(),
  resolved_at: z.number(),
  total_overridden: z.boolean(),
});
const recordClocks = z.strictObject({
  monotonic_start: z.number(),
  utc_start: z.string(),
  ntp_offset_s: z.number().nullable(),
  wall_trusted: z.boolean().nullable().optional(),
});
const recordGeometry = z.strictObject({
  reference_radius_m: z.number(),
  leg_tip_radius_m: z.number(),
  leg_tip_measured: z.boolean(),
  arm_tip_radius_m: z.number(),
  capsule_near_radius_m: z.number(),
  capsule_far_radius_m: z.number(),
  counterweight_radius_m: z.number(),
  gear_ratio: z.number(),
  nominal_motor_rpm: z.number(),
  base_hz: z.number(),
});
const endObservation = z.strictObject({
  t: z.number(),
  runtime_state: z.string().nullable().optional(),
  drive_state: z.string().nullable().optional(),
  runtime_output_enabled: z.boolean().nullable().optional(),
  runtime_applied_rpm: z.number().nullable().optional(),
  lfrd_motor_rpm: z.number().nullable().optional(),
  shaft_motor_rpm: z.number().nullable().optional(),
  energised: z.boolean().nullable().optional(),
  silent: z.boolean().nullable().optional(),
  stop_reason: z.string().nullable().optional(),
  shutdown_detail: z.string().nullable().optional(),
});
export const depositManifest = z
  .object({
    schema_version: z.literal(2),
    ended_at: z.string().min(1),
    files: z.array(recordPath).min(3).max(MAX_RECORD_FILES),
    record_id: z.string().optional(),
    machine_id: z.string().optional(),
    organization_id: z.string().optional(),
    session_id: z.string().nullable().optional(),
    local_ref: z.string().optional(),
    kind: z.enum(["auto", "manual"]).optional(),
    occupancy: z.enum(["bench", "occupied"]).optional(),
    operator: z.string().optional(),
    subject_id: z.string().nullable().optional(),
    profile: recordProfile.partial().nullable().optional(),
    config_hash: z.string().optional(),
    software_version: z.string().optional(),
    contract_version: z.string().optional(),
    medical_parameters_version: z.string().optional(),
    clocks: recordClocks.optional(),
    started_at: z.string().optional(),
    end_reason: z.string().nullable().optional(),
    preflight: z
      .array(z.strictObject({ check: z.string(), passed: z.boolean() }))
      .nullable()
      .optional(),
    geometry: recordGeometry.nullable().optional(),
    end_observation: endObservation.nullable().optional(),
    subject_fit: z.literal(true).nullable().optional(),
    fitness_questionnaire_version: z
      .string()
      .regex(/^[0-9A-Za-z.+-]{1,32}$/)
      .nullable()
      .optional(),
  })
  .strict()
  .refine(
    ({ files }) =>
      new Set(files).size === files.length &&
      new TextEncoder().encode(JSON.stringify(files)).length <= 1024 * 1024 &&
      ["manifest.json", "ticks.csv", "events.jsonl", "checksums.sha256"].every(
        (path) => files.includes(path),
      ),
  );

export function uploadMediaType(nonce: string): string {
  return RECORD_MEDIA_PREFIX + nonce;
}
export function storageNonce(contentType: string | undefined): string | null {
  if (!contentType?.startsWith(RECORD_MEDIA_PREFIX)) return null;
  const nonce = contentType.slice(RECORD_MEDIA_PREFIX.length);
  return /^[a-f0-9]{64}$/.test(nonce) ? nonce : null;
}
export function randomNonce(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
export async function sha256(bytes: ArrayBuffer): Promise<string> {
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}
