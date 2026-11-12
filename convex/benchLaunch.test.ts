/// <reference types="vite/client" />
/**
 * ANH-250: a ballasted bench session launched from the site.
 *
 * - EX-1: `launchBenchSession` queues a manual, remote session with no rider,
 *   the declared ballast and the launcher's note, for the staff who reach the
 *   machine (one row per role in `authorization.matrix.ts`); every refusal is
 *   a stable code;
 * - EX-2: the poll offers it, in its own shape, only to a console that
 *   announces contract 1.9 or later; the arming check reads no rider;
 * - the paths that handle a launch or a session read it as one: the sweep,
 *   the stop, the live view, the lists, the summary, and feedback, which a
 *   bench session never takes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { CONTRACT_HEADER, contractAtLeast } from "./lib/contract";
import { manifestContext } from "./lib/sessionSummary";
import { personOnBoard } from "./lib/machineRegistry";
import {
  ARMING_REFUSALS,
  DEFAULT_PENDING_SESSION_TTL_S,
  MAX_BALLAST_KG,
  MAX_LAUNCH_NOTES,
  POLL_MARGIN_S,
  settlePendingLaunchesForRegistration,
} from "./training";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld,
  seedMachineWorld,
  type World,
} from "./test.setup";

const SECOND = 1000;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

/** A world whose first machine's console announced `contract` (none: null). */
async function world(contract: string | null = "1.9") {
  const w = await seedLaunchWorld(modules);
  await w.t.run((ctx) =>
    ctx.db.patch(w.machine, { contractVersion: contract ?? undefined }),
  );
  return w;
}

function launch(
  w: World,
  args: { ballastKg?: number; notes?: string } = { ballastKg: 80 },
  actor: "manager" | "admin" | "orgAdmin" | "patient" = "manager",
  machineId: Id<"machines"> = w.machine,
) {
  return as(w.t, actor).mutation(api.training.launchBenchSession, {
    machineId,
    ...args,
  });
}

async function codeOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

const row = (w: Pick<World, "t">, id: Id<"sessions">) =>
  w.t.run((ctx) => ctx.db.get(id));

const offered = (
  w: Pick<World, "t">,
  machineId: Id<"machines">,
  contract?: string,
) =>
  w.t.query(internal.training.getPendingTrainingSession, {
    machineId,
    contract,
  });

const arm = (
  w: Pick<World, "t">,
  machineId: Id<"machines">,
  sessionId: Id<"sessions">,
) =>
  w.t.mutation(internal.training.markTrainingStarted, { machineId, sessionId });

describe("ANH-250 EX-1 a bench session is launched from the site", () => {
  it("queues a manual remote session with no rider, the ballast, the note and the launcher", async () => {
    const w = await world();
    const sessionId = await launch(w, {
      ballastKg: 80,
      notes: "  sac de sable  ",
    });
    expect(await row(w, sessionId)).toMatchObject({
      organizationId: w.orgA,
      machineId: w.machine,
      startedById: w.manager,
      status: "pending",
      startedAt: NOW,
      channels: ["ECG"],
      notes: "sac de sable",
      kind: "manual",
      origin: "remote",
      operatorName: "manager Synthetic",
      ballast: { massKg: 80 },
    });
    const session = await row(w, sessionId);
    expect(session?.userId).toBeUndefined();
    expect(session?.profileId).toBeUndefined();
    expect(session?.subjectHrMax).toBeUndefined();
  });

  it("keeps a mass not given as null, and an empty note as none", async () => {
    const w = await world();
    const sessionId = await launch(w, { notes: "   " });
    const session = await row(w, sessionId);
    expect(session?.ballast).toEqual({ massKg: null });
    expect(session?.notes).toBeUndefined();
  });

  it.each([0, -1, MAX_BALLAST_KG + 0.1, Number.NaN, Number.POSITIVE_INFINITY])(
    "refuses a mass of %s kg",
    async (ballastKg) => {
      const w = await world();
      expect(await codeOf(launch(w, { ballastKg }))).toBe(
        "invalid_ballast_mass",
      );
    },
  );

  it("takes a mass at the bound and a note at its length", async () => {
    const w = await world();
    const sessionId = await launch(w, {
      ballastKg: MAX_BALLAST_KG,
      notes: "n".repeat(MAX_LAUNCH_NOTES),
    });
    expect((await row(w, sessionId))?.ballast).toEqual({
      massKg: MAX_BALLAST_KG,
    });
  });

  it("refuses a note longer than its bound", async () => {
    const w = await world();
    expect(
      await codeOf(launch(w, { notes: "n".repeat(MAX_LAUNCH_NOTES + 1) })),
    ).toBe("notes_too_long");
  });

  it("needs neither programmes nor medical parameters: nobody is on board", async () => {
    const w = await world();
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, {
        programsEnabled: false,
        medicalParametersVersion: null,
      });
      for (const version of await ctx.db
        .query("medical_parameters_versions")
        .collect())
        await ctx.db.delete(version._id);
    });
    expect(await launch(w)).toBeTruthy();
  });

  it.each([
    [null, "machine_contract_too_old"],
    ["1.8", "machine_contract_too_old"],
    ["2.9", "machine_contract_too_old"],
    ["garbage", "machine_contract_too_old"],
    ["1.10", null],
  ] as const)(
    "a console that announced %s is answered %s",
    async (contract, code) => {
      const w = await world(contract);
      expect(await codeOf(launch(w))).toBe(code);
    },
  );

  it("refuses a machine deleted, of no organisation, offline, in session or held", async () => {
    const w = await world();
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: true }));
    expect(await codeOf(launch(w, undefined, "admin"))).toBe(
      "machine_not_found",
    );
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { isDeleted: false, organizationId: undefined }),
    );
    expect(await codeOf(launch(w, undefined, "admin"))).toBe(
      "machine_not_found",
    );
    await w.t.run((ctx) =>
      ctx.db.patch(w.machine, { organizationId: w.orgA, status: "offline" }),
    );
    expect(await codeOf(launch(w))).toBe("machine_offline");
    await w.t.run((ctx) => ctx.db.patch(w.machine, { status: "in_session" }));
    expect(await codeOf(launch(w))).toBe("machine_busy");
    await w.t.run((ctx) => ctx.db.patch(w.machine, { status: "online" }));
    await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    expect(await codeOf(launch(w))).toBe("machine_busy");
  });

  it("one launch at a time: a second bench launch, or an auto one, finds the machine busy", async () => {
    const w = await world();
    await launch(w);
    expect(await codeOf(launch(w))).toBe("machine_busy");
    expect(
      await codeOf(
        as(w.t, "manager").mutation(api.training.launchAutoSession, {
          machineId: w.machine,
          profileId: w.profileId,
          userId: w.patient,
        }),
      ),
    ).toBe("machine_busy");
  });

  it("fails on the way a launch that has waited too long, and takes the machine", async () => {
    const w = await world();
    const stale = await launch(w);
    vi.setSystemTime(NOW + DEFAULT_PENDING_SESSION_TTL_S * SECOND + 1);
    const fresh = await launch(w);
    expect(await row(w, stale)).toMatchObject({
      status: "failed",
      endCode: "expired_before_start",
    });
    expect((await row(w, fresh))?.status).toBe("pending");
  });
});

describe("ANH-250 EX-2 the poll offers it only to a console that reads it", () => {
  it("offers the bench shape to 1.9: no programme, no rider, no heart rate", async () => {
    const w = await world();
    const sessionId = await launch(w, { ballastKg: 80, notes: "lest" });
    expect(await offered(w, w.machine, "1.9")).toEqual({
      sessionId,
      mode: "bench",
      occupancy: "ballast",
      ballastKg: 80,
      notes: "lest",
      operatorName: "manager Synthetic",
    });
  });

  it("says null for a mass and a note not given", async () => {
    const w = await world();
    const sessionId = await launch(w, {});
    expect(await offered(w, w.machine, "1.12")).toMatchObject({
      sessionId,
      ballastKg: null,
      notes: null,
    });
  });

  it.each([undefined, "1.8", "1.0", "2.9"])(
    "never offers it to a poll that announces %s, and leaves it waiting",
    async (contract) => {
      const w = await world();
      const sessionId = await launch(w);
      expect(await offered(w, w.machine, contract)).toBeNull();
      expect((await row(w, sessionId))?.status).toBe("pending");
    },
  );

  it("still offers an auto launch to an older console, with its mode", async () => {
    const w = await world();
    const sessionId = await as(w.t, "manager").mutation(
      api.training.launchAutoSession,
      { machineId: w.machine, profileId: w.profileId, userId: w.patient },
    );
    expect(await offered(w, w.machine, "1.8")).toMatchObject({
      sessionId,
      mode: "auto",
      profileId: w.profileId,
      subjectId: w.patient,
    });
  });

  it("is not offered once it expires within the poll margin", async () => {
    const w = await world();
    await launch(w);
    vi.setSystemTime(
      NOW + (DEFAULT_PENDING_SESSION_TTL_S - POLL_MARGIN_S) * SECOND + 1,
    );
    expect(await offered(w, w.machine, "1.9")).toBeNull();
  });

  it("goes over HTTP to the console that announces 1.9, and not to one of 1.8", async () => {
    const m = await seedMachineWorld(modules);
    await m.t.run((ctx) => ctx.db.patch(m.machine, { contractVersion: "1.9" }));
    const sessionId = await m.admin.mutation(api.training.launchBenchSession, {
      machineId: m.machine,
      ballastKg: 80,
    });
    const poll = (contract: string) =>
      m.t.fetch("/api/machine/training/poll", {
        method: "GET",
        headers: {
          Authorization: `Bearer ${m.machineKey}`,
          [CONTRACT_HEADER]: contract,
        },
      });
    const old = await poll("1.8");
    expect(old.status).toBe(200);
    expect(((await old.json()) as { session: unknown }).session).toBeNull();
    vi.setSystemTime(NOW + 3 * SECOND);
    const current = await poll("1.9");
    expect(((await current.json()) as { session: unknown }).session).toEqual({
      sessionId,
      mode: "bench",
      occupancy: "ballast",
      ballastKg: 80,
      notes: null,
      operatorName: expect.any(String) as unknown,
    });
  });
});

describe("ANH-250 the arming check of a bench launch reads no rider", () => {
  it("arms it: active, its machine in session", async () => {
    const w = await world();
    const sessionId = await launch(w);
    expect(await arm(w, w.machine, sessionId)).toEqual({ armed: true });
    expect((await row(w, sessionId))?.status).toBe("active");
    expect((await w.t.run((ctx) => ctx.db.get(w.machine)))?.status).toBe(
      "in_session",
    );
  });

  it.each(["manager", "admin", "orgAdmin"] as const)(
    "arms it for its launcher %s, whatever the patients' fitness",
    async (actor) => {
      const w = await world();
      const sessionId = await launch(w, { ballastKg: 80 }, actor);
      await w.t.run(async (ctx) => {
        for (const consent of await ctx.db.query("consents").collect())
          await ctx.db.delete(consent._id);
        for (const assessment of await ctx.db
          .query("fitness_assessments")
          .collect())
          await ctx.db.delete(assessment._id);
      });
      expect(await arm(w, w.machine, sessionId)).toEqual({ armed: true });
    },
  );

  it("refuses it when its launcher no longer reaches the machine", async () => {
    const w = await world();
    const sessionId = await launch(w);
    await w.t.run(async (ctx) => {
      for (const link of await ctx.db.query("machine_gestionnaires").collect())
        await ctx.db.delete(link._id);
    });
    expect(await offered(w, w.machine, "1.9")).toBeNull();
    expect(await arm(w, w.machine, sessionId)).toEqual({
      armed: false,
      code: "right_revoked_before_start",
      message: ARMING_REFUSALS.right_revoked_before_start,
    });
    expect(await row(w, sessionId)).toMatchObject({
      status: "failed",
      endCode: "right_revoked_before_start",
    });
  });

  it("refuses it when its launcher is gone or was never said", async () => {
    const w = await world();
    const gone = await launch(w);
    await w.t.run((ctx) => ctx.db.patch(w.manager, { clerkDeletedAt: NOW }));
    expect((await arm(w, w.machine, gone)).armed).toBe(false);
    const unsaid = await w.t.run((ctx) =>
      ctx.db.insert("sessions", {
        organizationId: w.orgA,
        machineId: w.otherMachine,
        status: "pending",
        startedAt: NOW,
        channels: ["ECG"],
        kind: "manual",
        origin: "remote",
        ballast: { massKg: null },
      }),
    );
    expect(await arm(w, w.otherMachine, unsaid)).toMatchObject({
      armed: false,
      code: "right_revoked_before_start",
    });
  });

  it("refuses it when another open session holds the machine", async () => {
    const w = await world();
    const sessionId = await launch(w);
    await addSession(w, {
      machineId: w.machine,
      status: "active",
      kind: "manual",
    });
    expect(await offered(w, w.machine, "1.9")).toBeNull();
    expect(await arm(w, w.machine, sessionId)).toEqual({
      armed: false,
      code: "machine_busy",
      message: ARMING_REFUSALS.machine_busy,
    });
  });

  it("refuses it expired, its machine deleted, or its machine in another organisation", async () => {
    const w = await world();
    const expired = await launch(w);
    vi.setSystemTime(NOW + DEFAULT_PENDING_SESSION_TTL_S * SECOND + 1);
    expect(await arm(w, w.machine, expired)).toMatchObject({
      code: "expired_before_start",
    });
    const deleted = await launch(w);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: true }));
    expect(await arm(w, w.machine, deleted)).toMatchObject({
      code: "machine_deleted_before_start",
    });
    await w.t.run((ctx) => ctx.db.patch(w.machine, { isDeleted: false }));
    const moved = await launch(w);
    await w.t.run((ctx) => ctx.db.patch(w.machine, { organizationId: w.orgB }));
    expect(await arm(w, w.machine, moved)).toMatchObject({
      code: "organization_changed_before_start",
    });
  });

  it("is failed by the sweep when it can no longer be armed", async () => {
    const w = await world();
    const sessionId = await launch(w);
    expect(
      await w.t.mutation(internal.training.sweepPendingSessions, {}),
    ).toEqual({ failed: 0 });
    vi.setSystemTime(NOW + DEFAULT_PENDING_SESSION_TTL_S * SECOND + 1);
    expect(
      await w.t.mutation(internal.training.sweepPendingSessions, {}),
    ).toEqual({ failed: 1 });
    expect((await row(w, sessionId))?.endCode).toBe("expired_before_start");
  });

  it("does not hold the registration of a medical parameters version", async () => {
    const w = await world();
    await launch(w);
    expect(
      await w.t.run((ctx) => settlePendingLaunchesForRegistration(ctx, NOW)),
    ).toBe(0);
  });
});

describe("ANH-250 the site follows, stops and reports a bench session", () => {
  it("cancels it while it waits, and asks a running one to stop", async () => {
    const w = await world();
    const waiting = await launch(w);
    await as(w.t, "manager").mutation(api.training.requestStop, {
      sessionId: waiting,
    });
    expect(await row(w, waiting)).toMatchObject({ status: "failed" });

    const running = await launch(w);
    await arm(w, w.machine, running);
    await as(w.t, "orgAdmin").mutation(api.training.requestStop, {
      sessionId: running,
    });
    expect((await row(w, running))?.stopRequestedAt).toBe(NOW);
    expect(
      await w.t.query(internal.training.getTrainingStatus, {
        machineId: w.machine,
        sessionId: running,
      }),
    ).toEqual({ status: "active", active: true, stopRequested: true });
  });

  it("is not stopped by a patient, who cannot read it", async () => {
    const w = await world();
    const sessionId = await launch(w);
    expect(
      await codeOf(
        as(w.t, "patient").mutation(api.training.requestStop, { sessionId }),
      ),
    ).toBe("session_not_managed");
  });

  it("shows its ballast on the live view and in the lists", async () => {
    const w = await world();
    const sessionId = await launch(w, { ballastKg: 75.5 });
    expect(
      await as(w.t, "manager").query(api.training.getTrainingSession, {
        sessionId,
      }),
    ).toMatchObject({
      kind: "manual",
      origin: "remote",
      ballast: { massKg: 75.5 },
      canStop: true,
    });
    const rows = await as(w.t, "manager").query(api.sessions.listSessions, {});
    expect(rows.find((listed) => listed._id === sessionId)).toMatchObject({
      kind: "manual",
      origin: "remote",
      ballast: { massKg: 75.5 },
    });
  });

  it("ends as any session, with a provisional summary that keeps its ballast", async () => {
    const w = await world();
    const sessionId = await launch(w, { ballastKg: 80 });
    await arm(w, w.machine, sessionId);
    await w.t.mutation(internal.training.storeTelemetry, {
      machineId: w.machine,
      sessionId,
      points: [
        {
          t: NOW + SECOND,
          elapsedS: 1,
          phase: "hold",
          motorRpm: 300,
          outputRpm: 6,
          setpointMotorRpm: 300,
          gLoad: 1.01,
          safetyAction: "none",
        },
      ],
    });
    await w.t.mutation(internal.training.endTrainingSession, {
      machineId: w.machine,
      sessionId,
      failed: false,
      reason: "operator_stop",
    });
    await w.t.finishAllScheduledFunctions(vi.runAllTimers);
    expect((await row(w, sessionId))?.status).toBe("completed");
    const summary = await as(w.t, "manager").query(
      api.sessionReports.getSummary,
      { sessionId },
    );
    expect(summary).toMatchObject({
      provisional: true,
      figures: { ballast: { massKg: 80 } },
    });
  });

  it("reads the ballast of a record's manifest, and none from an old one", () => {
    expect(manifestContext({ ballast: { mass_kg: 80 } })?.ballast).toEqual({
      massKg: 80,
    });
    expect(manifestContext({ ballast: { mass_kg: null } })?.ballast).toEqual({
      massKg: null,
    });
    expect(manifestContext({})?.ballast).toBeNull();
    expect(manifestContext({ ballast: "80 kg" })?.ballast).toBeNull();
  });

  it("never had anyone on board, whatever its note says: no feedback", async () => {
    expect(
      personOnBoard({
        kind: "manual",
        notes: "Occupancy: occupied",
        ballast: { massKg: 80 },
      }),
    ).toBe(false);
    const w = await world();
    const sessionId = await launch(w, { notes: "Occupancy: occupied" });
    await arm(w, w.machine, sessionId);
    await w.t.mutation(internal.training.endTrainingSession, {
      machineId: w.machine,
      sessionId,
      failed: false,
      reason: "operator_stop",
    });
    expect(
      await as(w.t, "manager").query(api.sessionFeedback.getSessionFeedback, {
        sessionId,
      }),
    ).toMatchObject({ eligible: false });
  });

  it("reads a version against the first minor that takes a bench launch", () => {
    expect(contractAtLeast("1.9", "1.9")).toBe(true);
    expect(contractAtLeast("1.10", "1.9")).toBe(true);
    expect(contractAtLeast("1.8", "1.9")).toBe(false);
    expect(contractAtLeast("2.9", "1.9")).toBe(false);
    expect(contractAtLeast(null, "1.9")).toBe(false);
    expect(contractAtLeast(undefined, "1.9")).toBe(false);
    expect(contractAtLeast("1.9", "not a version")).toBe(false);
  });
});
