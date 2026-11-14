import { v } from "convex/values";
import { z } from "zod";

const measurement = v.union(v.number(), v.null());
const reading = z.number().finite().nullable();
const count = z.number().int().nonnegative().nullable();
const link = v.union(v.literal("up"), v.literal("down"), v.null());

/**
 * Contract 1.14 (`health_state` of contracts/machine-api.json, ANH-92,
 * ANH-148): the safety chain as the console's last tick read it, and whether
 * a time service keeps its wall clock. Each optional: an earlier console
 * sends none of them. Kept and shown, never decided on.
 */
export const STO_STATES = [
  "ready",
  "open",
  "unknown",
  "not_configured",
  "bridged",
] as const;
export const STO_SOURCES = [
  "none",
  "sim_ready",
  "sim_open",
  "drive_input",
  "bridged_declared",
] as const;
const stoState = v.union(...STO_STATES.map((s) => v.literal(s)), v.null());
const stoSource = v.union(...STO_SOURCES.map((s) => v.literal(s)), v.null());

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
  sto_state: v.optional(stoState),
  sto_source: v.optional(stoSource),
  clock_trusted: v.optional(v.union(v.boolean(), v.null())),
  clock_max_error_s: v.optional(measurement),
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
  sto_state: z.enum(STO_STATES).nullable().optional(),
  sto_source: z.enum(STO_SOURCES).nullable().optional(),
  clock_trusted: z.boolean().nullable().optional(),
  clock_max_error_s: z.number().finite().nonnegative().nullable().optional(),
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
