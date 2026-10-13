import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Infer } from "convex/values";
import { api, internal } from "./_generated/api";
import { as, modules, NOW, seedMachineWorld, seedWorld } from "./test.setup";
import {
  machineHealthValidator,
  HEARTBEAT_RETENTION_MS,
  HEALTH_RETENTION_MS,
} from "./lib/machineHealth";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";

const reading = (cpu: number | null): Infer<typeof machineHealthValidator> => ({
  cpu_temp_c: cpu,
  cpu_load_1m: 0.4,
  mem_used_pct: 35,
  disk_free_mb: 1200,
  uptime_s: 20,
  clock_offset_s: null,
  link_rtt_ms: 5,
  bitalino_battery_pct: null,
  drive_thermal_state: null,
  records_pending: 2,
  incidents_open_high: null,
  bitalino_link: "up",
  drive_link: "up",
  record_degraded: false,
  restart_loop: false,
  preflight_last: null,
  counters: {
    rotation_hours: 0,
    sessions_total: 0,
    sessions_failed: 0,
    drive_faults_by_code: {},
  },
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-148 machine health", () => {
  it("aggregates a five-minute bucket and preserves unknown latest readings", async () => {
    const w = await seedWorld(modules);
    for (const [delay, cpu] of [
      [0, 40],
      [10_000, 80],
      [20_000, null],
    ] as const) {
      vi.setSystemTime(NOW + delay);
      await w.t.mutation(internal.machines.recordHeartbeat, {
        machineId: w.machine,
        health: reading(cpu),
      });
    }
    const result = await as(w.t, "manager").query(api.machineHealth.get, {
      machineId: w.machine,
    });
    expect(result?.health?.cpu_temp_c).toBeNull();
    expect(result?.history).toHaveLength(1);
    expect(result?.history[0]).toMatchObject({
      samples: 3,
      min: { cpu_temp_c: 40 },
      max: { cpu_temp_c: 80 },
    });
    vi.setSystemTime(NOW + 300_000);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
      health: reading(60),
    });
    expect(
      (
        await as(w.t, "manager").query(api.machineHealth.get, {
          machineId: w.machine,
        })
      )?.history,
    ).toHaveLength(2);
  });

  it("does not expose health to another organization or an unassigned manager", async () => {
    const w = await seedWorld(modules);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
      health: reading(70),
    });
    for (const actor of ["orgBAdmin", "orgBManager", "otherManager"] as const) {
      expect(
        await as(w.t, actor).query(api.machineHealth.get, {
          machineId: w.machine,
        }),
      ).toBeNull();
    }
    await expect(
      w.t.query(api.machineHealth.get, { machineId: w.machine }),
    ).rejects.toThrow();
  });

  it("clears obsolete latest health when a legacy heartbeat omits it", async () => {
    const w = await seedWorld(modules);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
      health: reading(70),
    });
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
    });
    const result = await as(w.t, "manager").query(api.machineHealth.get, {
      machineId: w.machine,
    });
    expect(result?.health).toBeNull();
    expect(result?.updatedAt).toBeNull();
    expect(result?.history).toHaveLength(1);
  });

  it("accepts authenticated health and refuses malformed measurements without a write", async () => {
    const w = await seedMachineWorld(modules);
    const send = (health: unknown) =>
      w.t.fetch("/api/machine/heartbeat", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          [CONTRACT_HEADER]: CONTRACT_VERSION,
        },
        body: JSON.stringify({ health }),
      });
    expect((await send(reading(65))).status).toBe(200);
    for (const health of [
      { ...reading(65), mem_used_pct: 101 },
      { ...reading(65), records_pending: -1 },
      null,
      { cpu_temp_c: 10 },
    ]) {
      expect((await send(health)).status).toBe(400);
    }
    const stored = await w.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(stored).toHaveLength(1);
    expect(
      (await w.t.run((ctx) => ctx.db.get(w.machine)))?.health?.cpu_temp_c,
    ).toBe(65);
  });

  it("keeps no heartbeat older than 24 hours in an active machine's normal stream", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let n = 0; n < 8640; n++) {
        await ctx.db.insert("machine_heartbeats", {
          machineId: w.machine,
          timestamp: NOW - HEARTBEAT_RETENTION_MS + n * 10_000,
        });
      }
    });
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
      health: reading(50),
    });
    const rows = await w.t.run((ctx) =>
      ctx.db.query("machine_heartbeats").collect(),
    );
    expect(rows).toHaveLength(8640);
    expect(Math.min(...rows.map((row) => row.timestamp))).toBeGreaterThan(
      NOW - HEARTBEAT_RETENTION_MS,
    );
  });

  it("purges old heartbeats and 90-day buckets in bounded batches while retaining recent history", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let n = 0; n < 101; n++) {
        await ctx.db.insert("machine_heartbeats", {
          machineId: w.machine,
          timestamp: NOW - HEARTBEAT_RETENTION_MS - n,
        });
      }
    });
    vi.setSystemTime(NOW - HEALTH_RETENTION_MS - 300_000);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.machine,
      health: reading(20),
    });
    vi.setSystemTime(NOW);
    await w.t.mutation(internal.machines.recordHeartbeat, {
      machineId: w.otherMachine,
      health: reading(50),
    });
    const first = await w.t.mutation(internal.machineHealth.purge, {});
    expect(first).toEqual({ heartbeats: 100, buckets: 1, more: true });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect(
      await w.t.run((ctx) => ctx.db.query("machine_heartbeats").collect()),
    ).toHaveLength(1);
    expect(
      await w.t.run((ctx) => ctx.db.query("machine_health_history").collect()),
    ).toHaveLength(1);
  });
});
