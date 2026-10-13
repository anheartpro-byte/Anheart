import { v } from "convex/values";
import { z } from "zod";

const measurement = v.union(v.number(), v.null());
const reading = z.number().finite().nullable();
const count = z.number().int().nonnegative().nullable();
const link = v.union(v.literal("up"), v.literal("down"), v.null());

export const healthMeasurements = {
  cpu_temp_c: measurement,
  cpu_load_1m: measurement,
  mem_used_pct: measurement,
  disk_free_mb: measurement,
  uptime_s: measurement,
  clock_offset_s: measurement,
  link_rtt_ms: measurement,
  bitalino_battery_pct: measurement,
  drive_thermal_state: measurement,
  records_pending: measurement,
  incidents_open_high: measurement,
};

export const healthMeasurementsValidator = v.object(healthMeasurements);
export const machineHealthValidator = v.object({
  ...healthMeasurements,
  bitalino_link: link,
  drive_link: link,
  record_degraded: v.union(v.boolean(), v.null()),
  restart_loop: v.union(v.boolean(), v.null()),
  preflight_last: v.union(
    v.literal("ok"),
    v.literal("warn"),
    v.literal("block"),
    v.null(),
  ),
  counters: v.object({
    rotation_hours: measurement,
    sessions_total: measurement,
    sessions_failed: measurement,
    drive_faults_by_code: v.union(v.record(v.string(), v.number()), v.null()),
  }),
});

export const machineHealthSchema = z.strictObject({
  cpu_temp_c: reading,
  cpu_load_1m: z.number().nonnegative().nullable(),
  mem_used_pct: z.number().min(0).max(100).nullable(),
  disk_free_mb: z.number().nonnegative().nullable(),
  uptime_s: z.number().nonnegative().nullable(),
  clock_offset_s: reading,
  link_rtt_ms: z.number().nonnegative().nullable(),
  bitalino_battery_pct: z.number().min(0).max(100).nullable(),
  drive_thermal_state: reading,
  records_pending: count,
  incidents_open_high: count,
  bitalino_link: z.enum(["up", "down"]).nullable(),
  drive_link: z.enum(["up", "down"]).nullable(),
  record_degraded: z.boolean().nullable(),
  restart_loop: z.boolean().nullable(),
  preflight_last: z.enum(["ok", "warn", "block"]).nullable(),
  counters: z.strictObject({
    rotation_hours: z.number().nonnegative().nullable(),
    sessions_total: count,
    sessions_failed: count,
    drive_faults_by_code: z
      .record(
        z.string().regex(/^[A-Za-z0-9_:-]{1,32}$/),
        z.number().int().nonnegative(),
      )
      .refine((codes) => Object.keys(codes).length <= 128)
      .nullable(),
  }),
});

export const HEALTH_BUCKET_MS = 5 * 60 * 1000;
export const HEARTBEAT_RETENTION_MS = 24 * 60 * 60 * 1000;
export const HEALTH_RETENTION_MS = 90 * HEARTBEAT_RETENTION_MS;
