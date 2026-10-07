/// <reference types="vite/client" />
/**
 * ANH-135: the ECG recording mode is retired from the backend.
 *
 * One mode remains, the training sessions of the local console. This suite
 * checks what its removal means here:
 *
 * - the five machine routes of the recording mode no longer exist, and no
 *   function creates, starts, feeds or ends a recording session;
 * - `ecg_data` and `session_summaries` are read-only history;
 * - ending a training session schedules nothing;
 * - a machine carries no recorder settings, and `kind: "recording"` is not a
 *   value the schema accepts;
 * - the data migration closes the recording sessions left open, strips the
 *   recorder settings from the machines, and touches nothing else.
 *
 * Everything runs in memory with `convex-test`: no deployment is involved.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import http from "./http";
import { machineHeaders } from "./machineAuth.fixtures";
import { LEGACY_END_REASON } from "./migrations/retireLegacyRecording";
import {
  addSession,
  as,
  modules,
  NOW,
  seedMachineWorld,
  seedWorld,
  WORLD_MACHINE_COUNT,
} from "./test.setup";

// Every deployable source of the backend, as text.
const sources = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
  ],
  { eager: true, query: "?raw", import: "default" },
) as Record<string, string>;

const MIGRATION = "./migrations/retireLegacyRecording.ts";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
});

describe("ANH-135 the recording routes are gone", () => {
  const retired = [
    ["GET", "/api/machine/session/poll"],
    ["POST", "/api/machine/session/start"],
    ["POST", "/api/machine/session/end"],
    ["GET", "/api/machine/session/status"],
    ["POST", "/api/machine/data"],
  ] as const;

  it("registers only the heartbeat, the roster, the profiles and the training routes", () => {
    const paths = http.getRoutes().map(([path]) => path);
    expect(paths).toHaveLength(9);
    for (const path of paths) {
      expect(path).toMatch(
        /^\/api\/machine\/(heartbeat|roster|profiles|training\/[a-z]+)$/,
      );
    }
  });

  it.each(retired)(
    "%s %s answers 404, even to a machine with a valid key",
    async (method, path) => {
      const w = await seedMachineWorld(modules);
      const response = await w.t.fetch(path, {
        method,
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          "Content-Type": "application/json",
        },
        body: method === "POST" ? JSON.stringify({}) : undefined,
      });
      expect(response.status).toBe(404);
    },
  );
});

describe("ANH-135 no function belongs to the recording mode any more", () => {
  it("found the sources it reads", () => {
    expect(Object.keys(sources)).toContain("./sessions.ts");
    expect(Object.keys(sources)).toContain("./lib/machineAuth.ts");
    expect(Object.keys(sources)).toContain(MIGRATION);
  });

  it("keeps the sessions module to its four readers", async () => {
    const sessions = await import("./sessions");
    expect(Object.keys(sessions).sort()).toEqual([
      "getActiveSessionForMachine",
      "getCompletedSessionsForUser",
      "getSession",
      "listSessions",
    ]);
  });

  it("keeps only readers of the ECG history", async () => {
    const ecgData = await import("./ecgData");
    const summaries = await import("./sessionSummaries");
    expect(Object.keys(ecgData).sort()).toEqual([
      "getLatestEcgBatch",
      "getRecentEcgData",
      "getSessionAllData",
      "getSessionDataStats",
      "getSessionEcgRange",
    ]);
    expect(Object.keys(summaries).sort()).toEqual([
      "getSummary",
      "getSummaryWithEcg",
    ]);
    for (const [name, registered] of [
      ...Object.entries(ecgData),
      ...Object.entries(summaries),
    ]) {
      expect((registered as { isQuery?: boolean }).isQuery, name).toBe(true);
    }
  });

  it("writes to neither ecg_data nor session_summaries anywhere", () => {
    const writers = Object.entries(sources).filter(([, text]) =>
      /\.insert\(\s*"(ecg_data|session_summaries)"/.test(text),
    );
    expect(writers.map(([path]) => path)).toEqual([]);
  });

  it("creates a session in two places only: a dashboard launch and a machine registration", () => {
    const inserting = Object.entries(sources)
      .filter(([, text]) => /\.insert\(\s*"sessions"/.test(text))
      .map(([path]) => path);
    expect(inserting).toEqual(["./training.ts"]);
    const count = sources["./training.ts"].match(/\.insert\(\s*"sessions"/g);
    expect(count).toHaveLength(2);
  });

  it("names the recording mode nowhere but in the migration and the history readers", () => {
    const naming = Object.entries(sources)
      .filter(([, text]) =>
        /generateSummary|storeEcgBatch|createSession|endSessionInternal|failSession|getPendingSessionForMachine|cancelSession|\/api\/machine\/session|\/api\/machine\/data/.test(
          text,
        ),
      )
      .map(([path]) => path);
    expect(naming).toEqual([]);
  });

  it("reads the recorder settings of a machine nowhere", () => {
    const reading = Object.entries(sources)
      .filter(
        ([path, text]) =>
          path !== MIGRATION &&
          path !== "./schema.ts" &&
          /\.config\b|sampleRate:\s*machine|batchInterval/.test(text),
      )
      .map(([path]) => path);
    expect(reading).toEqual([]);
  });
});

describe("ANH-135 the schema follows", () => {
  it('refuses a session of kind "recording"', async () => {
    const w = await seedWorld(modules);
    await expect(
      w.t.run((ctx) =>
        ctx.db.insert("sessions", {
          machineId: w.machine,
          status: "completed" as const,
          startedAt: NOW,
          channels: ["ECG"],
          // The cast is the point: the type no longer allows the value either.
          kind: "recording" as unknown as "auto",
        }),
      ),
    ).rejects.toThrow();
  });

  it("still reports a session without kind as a recording, read-only", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    const session = await as(w.t, "manager").query(api.sessions.getSession, {
      sessionId,
    });
    expect(session?.kind).toBe("recording");
    const listed = await as(w.t, "manager").query(
      api.sessions.listSessions,
      {},
    );
    expect(listed.find((row) => row._id === sessionId)?.kind).toBe("recording");
  });

  it("creates a machine with no recorder settings, and returns none", async () => {
    const w = await seedWorld(modules);
    const created = await as(w.t, "admin").mutation(
      api.machines.createMachine,
      {
        name: "Synthetic new machine",
      },
    );
    const stored = await w.t.run((ctx) => ctx.db.get(created.machineId));
    expect(stored).not.toHaveProperty("config");
    const read = await as(w.t, "admin").query(api.machines.getMachine, {
      machineId: created.machineId,
    });
    expect(read).not.toBeNull();
    expect(read).not.toHaveProperty("config");
  });

  it("authenticates a machine that carries no recorder settings", async () => {
    const w = await seedMachineWorld(modules);
    expect(await w.t.run((ctx) => ctx.db.get(w.machine))).not.toHaveProperty(
      "config",
    );
    const response = await w.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: machineHeaders(w.machineKey),
    });
    expect(response.status).toBe(200);
  });
});

describe("ANH-135 a training session writes no ECG field and no summary", () => {
  it("launches an auto session without a sample rate", async () => {
    const w = await seedWorld(modules);
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.kind).toBe("auto");
    expect(session).not.toHaveProperty("sampleRate");
  });

  it("registers a session started at the machine without a sample rate", async () => {
    const w = await seedWorld(modules);
    const sessionId = await w.t.mutation(
      internal.training.registerLocalSession,
      {
        machineId: w.machine,
        localRef: "local-ref-anh-135",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Synthetic Operator",
      },
    );
    const session = await w.t.run((ctx) => ctx.db.get(sessionId));
    expect(session?.kind).toBe("manual");
    expect(session).not.toHaveProperty("sampleRate");
  });

  it.each([{ failed: false }, { failed: true }])(
    "schedules nothing when a training session ends (failed: $failed)",
    async ({ failed }) => {
      const w = await seedWorld(modules);
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      await w.t.mutation(internal.training.endTrainingSession, {
        machineId: w.machine,
        sessionId,
        failed,
        reason: failed ? "synthetic reason" : "programme_complete",
      });
      const scheduled = await w.t.run((ctx) =>
        ctx.db.system.query("_scheduled_functions").collect(),
      );
      expect(scheduled).toEqual([]);
      await w.t.finishAllScheduledFunctions(vi.runAllTimers);
      expect(
        await w.t.run((ctx) => ctx.db.query("session_summaries").collect()),
      ).toEqual([]);
      const ended = await w.t.run((ctx) => ctx.db.get(sessionId));
      expect(ended?.status).toBe(failed ? "failed" : "completed");
    },
  );
});

describe("ANH-135 migration: recording sessions left open are marked failed", () => {
  const migrate = (w: Awaited<ReturnType<typeof seedWorld>>) =>
    w.t.mutation(
      internal.migrations.retireLegacyRecording.failOpenRecordingSessions,
      {},
    );
  const read = (w: Awaited<ReturnType<typeof seedWorld>>, id: Id<"sessions">) =>
    w.t.run((ctx) => ctx.db.get(id));

  it("fails a pending and an active recording session, with the reason", async () => {
    const w = await seedWorld(modules);
    const pending = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
    });
    const active = await addSession(w, {
      machineId: w.otherMachine,
      userId: w.otherPatient,
      status: "active",
    });

    const result = await migrate(w);

    expect(result).toEqual({ machinesChecked: WORLD_MACHINE_COUNT, sessionsFailed: 2 });
    for (const id of [pending, active]) {
      const session = await read(w, id);
      expect(session?.status).toBe("failed");
      expect(session?.endReason).toBe(LEGACY_END_REASON);
      expect(session?.endReason).toBe("legacy mode retired");
      expect(session?.endedAt).toBe(NOW);
      // The row keeps its shape: it is still a recording session.
      expect(session).not.toHaveProperty("kind");
    }
  });

  it("leaves finished recording sessions exactly as they were", async () => {
    const w = await seedWorld(modules);
    const completed = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
    });
    const failed = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "failed",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(failed, {
        endedAt: NOW - 1000,
        notes: "Failure reason: synthetic",
      }),
    );
    const before = [await read(w, completed), await read(w, failed)];

    const result = await migrate(w);

    expect(result.sessionsFailed).toBe(0);
    expect([await read(w, completed), await read(w, failed)]).toEqual(before);
  });

  it("leaves every training session untouched, open or not", async () => {
    const w = await seedWorld(modules);
    const training: Id<"sessions">[] = [];
    for (const kind of ["auto", "manual"] as const) {
      for (const status of [
        "pending",
        "active",
        "completed",
        "failed",
      ] as const) {
        training.push(
          await addSession(w, {
            machineId: w.machine,
            userId: w.patient,
            status,
            kind,
          }),
        );
      }
    }
    const before = await Promise.all(training.map((id) => read(w, id)));

    const result = await migrate(w);

    expect(result.sessionsFailed).toBe(0);
    expect(await Promise.all(training.map((id) => read(w, id)))).toEqual(
      before,
    );
  });

  it("does not write the machine: its status follows the heartbeats", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { status: "in_session" }));
    await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
    });
    const before = await w.t.run((ctx) => ctx.db.get(w.machine));

    await migrate(w);

    expect(await w.t.run((ctx) => ctx.db.get(w.machine))).toEqual(before);
  });

  it("reaches the sessions of a deleted machine too", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { isDeleted: true, deletedAt: NOW }),
    );
    const pending = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
    });

    await migrate(w);

    expect((await read(w, pending))?.status).toBe("failed");
  });

  it("changes nothing on a second run", async () => {
    const w = await seedWorld(modules);
    const pending = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
    });
    await migrate(w);
    const after = await read(w, pending);

    vi.setSystemTime(NOW + 60_000);
    const second = await migrate(w);

    expect(second).toEqual({ machinesChecked: WORLD_MACHINE_COUNT, sessionsFailed: 0 });
    expect(await read(w, pending)).toEqual(after);
  });

  it("frees a machine a forgotten recording session was blocking", async () => {
    const w = await seedWorld(modules);
    await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "pending",
    });
    const launch = () =>
      as(w.t, "manager").mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
        userId: w.patient,
      });
    // Before: the pending recording session refuses every launch and blocks
    // the deletion of the machine.
    await expect(launch()).rejects.toThrow(/already waiting/);
    await expect(
      as(w.t, "admin").mutation(api.machines.deleteMachine, {
        machineId: w.machine,
      }),
    ).rejects.toThrow(/pending sessions/);

    await migrate(w);

    const launched = await launch();
    expect((await read(w, launched))?.kind).toBe("auto");
    await as(w.t, "manager").mutation(api.training.requestStop, {
      sessionId: launched,
    });
    await as(w.t, "admin").mutation(api.machines.deleteMachine, {
      machineId: w.machine,
    });
    expect((await w.t.run((ctx) => ctx.db.get(w.machine)))?.isDeleted).toBe(
      true,
    );
  });
});

describe("ANH-135 migration: the recorder settings leave the machines", () => {
  const CONFIG = { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 };
  const migrate = (w: Awaited<ReturnType<typeof seedWorld>>) =>
    w.t.mutation(
      internal.migrations.retireLegacyRecording.removeMachineConfig,
      {},
    );

  it("accepts a machine document that still carries them", async () => {
    // What every deployment holds today: the schema must take it as it is.
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { config: CONFIG }));
    expect((await w.t.run((ctx) => ctx.db.get(w.machine)))?.config).toEqual(
      CONFIG,
    );
  });

  it("removes the field and nothing else, deleted machines included", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, { config: CONFIG });
      await ctx.db.patch(w.otherMachine, {
        config: { ...CONFIG, sampleRate: 100 },
        isDeleted: true,
        deletedAt: NOW,
      });
    });
    const before = await w.t.run(async (ctx) => [
      await ctx.db.get(w.machine),
      await ctx.db.get(w.otherMachine),
    ]);

    const result = await migrate(w);

    expect(result).toEqual({ machinesChecked: WORLD_MACHINE_COUNT, machinesCleared: 2 });
    const after = await w.t.run(async (ctx) => [
      await ctx.db.get(w.machine),
      await ctx.db.get(w.otherMachine),
    ]);
    for (const [index, machine] of after.entries()) {
      expect(machine).not.toHaveProperty("config");
      const rest = Object.fromEntries(
        Object.entries(before[index]!).filter(([field]) => field !== "config"),
      );
      expect(machine).toEqual(rest);
    }
  });

  it("changes nothing on a second run, nor on a machine that never had them", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { config: CONFIG }));

    expect(await migrate(w)).toEqual({
      machinesChecked: WORLD_MACHINE_COUNT,
      machinesCleared: 1,
    });
    const after = await w.t.run((ctx) => ctx.db.query("machines").collect());
    expect(await migrate(w)).toEqual({
      machinesChecked: WORLD_MACHINE_COUNT,
      machinesCleared: 0,
    });
    expect(await w.t.run((ctx) => ctx.db.query("machines").collect())).toEqual(
      after,
    );
  });

  it("leaves the machine able to authenticate", async () => {
    const w = await seedMachineWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { config: CONFIG }));
    await w.t.mutation(
      internal.migrations.retireLegacyRecording.removeMachineConfig,
      {},
    );
    const response = await w.t.fetch("/api/machine/heartbeat", {
      method: "POST",
      headers: machineHeaders(w.machineKey),
    });
    expect(response.status).toBe(200);
  });
});
