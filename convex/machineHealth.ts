import { v, type Infer } from "convex/values";
import { internalMutation, query, type MutationCtx } from "./_generated/server";
import type { Doc } from "./_generated/dataModel";
import { internal } from "./_generated/api";
import schema from "./schema";
import { canAccessMachine, getCurrentUserOrThrow } from "./lib/auth";
import {
  machineHealthValidator,
  HEALTH_BUCKET_MS,
  HEALTH_RETENTION_MS,
  HEARTBEAT_RETENTION_MS,
} from "./lib/machineHealth";

const METRICS = [
  "cpu_temp_c",
  "cpu_load_1m",
  "mem_used_pct",
  "disk_free_mb",
  "uptime_s",
  "clock_offset_s",
  "link_rtt_ms",
  "bitalino_battery_pct",
  "drive_thermal_state",
  "records_pending",
  "incidents_open_high",
] as const;
const PURGE_BATCH = 100;

function measurements(health: Infer<typeof machineHealthValidator>) {
  const {
    cpu_temp_c,
    cpu_load_1m,
    mem_used_pct,
    disk_free_mb,
    uptime_s,
    clock_offset_s,
    link_rtt_ms,
    bitalino_battery_pct,
    drive_thermal_state,
    records_pending,
    incidents_open_high,
  } = health;
  return {
    cpu_temp_c,
    cpu_load_1m,
    mem_used_pct,
    disk_free_mb,
    uptime_s,
    clock_offset_s,
    link_rtt_ms,
    bitalino_battery_pct,
    drive_thermal_state,
    records_pending,
    incidents_open_high,
  };
}

export async function recordMachineHealth(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  health: Infer<typeof machineHealthValidator>,
  now: number,
): Promise<void> {
  const bucket = Math.floor(now / HEALTH_BUCKET_MS) * HEALTH_BUCKET_MS;
  const previous = await ctx.db
    .query("machine_health_history")
    .withIndex("by_machine_and_bucket", (q) =>
      q.eq("machineId", machine._id).eq("bucket", bucket),
    )
    .unique();
  if (previous === null) {
    await ctx.db.insert("machine_health_history", {
      machineId: machine._id,
      organizationId: machine.organizationId,
      bucket,
      updatedAt: now,
      samples: 1,
      min: measurements(health),
      max: measurements(health),
      last: health,
    });
    return;
  }
  const minimum = { ...previous.min };
  const maximum = { ...previous.max };
  for (const key of METRICS) {
    const value = health[key];
    if (value === null) continue;
    const low = minimum[key];
    const high = maximum[key];
    minimum[key] = low === null ? value : Math.min(low, value);
    maximum[key] = high === null ? value : Math.max(high, value);
  }
  await ctx.db.patch(previous._id, {
    updatedAt: now,
    samples: previous.samples + 1,
    min: minimum,
    max: maximum,
    last: health,
  });
}

export const get = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.null(),
    v.object({
      health: v.union(machineHealthValidator, v.null()),
      updatedAt: v.union(v.number(), v.null()),
      serverNow: v.number(),
      history: v.array(schema.doc("machine_health_history")),
    }),
  ),
  handler: async (ctx, args) => {
    const user = await getCurrentUserOrThrow(ctx);
    const machine = await ctx.db.get(args.machineId);
    if (machine === null || !(await canAccessMachine(ctx, machine._id, user)))
      return null;
    if (machine.isDeleted && user.role !== "admin") return null;
    const now = Date.now();
    const firstBucket =
      Math.floor((now - HEARTBEAT_RETENTION_MS) / HEALTH_BUCKET_MS) *
      HEALTH_BUCKET_MS;
    const history = await ctx.db
      .query("machine_health_history")
      .withIndex("by_machine_and_bucket", (q) =>
        q.eq("machineId", machine._id).gte("bucket", firstBucket),
      )
      .order("desc")
      .take(289);
    return {
      health: machine.health ?? null,
      updatedAt: machine.healthUpdatedAt ?? null,
      serverNow: now,
      history: history.reverse(),
    };
  },
});

export const purge = internalMutation({
  args: {},
  returns: v.object({
    heartbeats: v.number(),
    buckets: v.number(),
    more: v.boolean(),
  }),
  handler: async (ctx) => {
    const now = Date.now();
    const heartbeats = await ctx.db
      .query("machine_heartbeats")
      .withIndex("by_timestamp", (q) =>
        q.lte("timestamp", now - HEARTBEAT_RETENTION_MS),
      )
      .take(PURGE_BATCH);
    const buckets = await ctx.db
      .query("machine_health_history")
      .withIndex("by_bucket", (q) => q.lt("bucket", now - HEALTH_RETENTION_MS))
      .take(PURGE_BATCH);
    for (const row of heartbeats) await ctx.db.delete(row._id);
    for (const row of buckets) await ctx.db.delete(row._id);
    const more =
      heartbeats.length === PURGE_BATCH || buckets.length === PURGE_BATCH;
    if (more) await ctx.scheduler.runAfter(0, internal.machineHealth.purge, {});
    return { heartbeats: heartbeats.length, buckets: buckets.length, more };
  },
});
