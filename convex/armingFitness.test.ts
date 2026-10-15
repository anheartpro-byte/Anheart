import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import { CURRENT_QUESTIONNAIRE } from "../lib/fitnessQuestionnaire";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { latestAssessment, type FitnessLaunchRefusal } from "./lib/fitness";
import {
  configureAnheartOrganization,
  modules,
  NOW,
  seedMachineWorld,
} from "./test.setup";

type World = Awaited<ReturnType<typeof seedMachineWorld>>;

const changes: ReadonlyArray<{
  name: string;
  code: FitnessLaunchRefusal;
  change: (w: World) => Promise<unknown>;
}> = [
  {
    name: "a new assessment replaces the validated fitness while pending",
    code: "rider_not_fit",
    change: (w) =>
      w.admin.mutation(api.fitness.submitFitnessAnswers, {
        userId: w.patient,
        questionnaireVersion: CURRENT_QUESTIONNAIRE.version,
        answers: Object.fromEntries(
          CURRENT_QUESTIONNAIRE.questions.map((question) => [
            question.id,
            question.outcomes.yes === "fit",
          ]),
        ),
      }),
  },
  {
    name: "fitness expires while the session is pending",
    code: "fitness_expired",
    change: async () => {
      vi.setSystemTime(NOW + 60_000);
    },
  },
  {
    name: "consent is withdrawn while pending",
    code: "consent_missing",
    change: (w) =>
      w.admin.mutation(api.fitness.withdrawConsent, { userId: w.patient }),
  },
];

function request(w: World, path: string, sessionId?: string) {
  return w.t.fetch(path, {
    method: sessionId === undefined ? "GET" : "POST",
    headers: {
      Authorization: `Bearer ${w.machineKey}`,
      [CONTRACT_HEADER]: CONTRACT_VERSION,
      "Content-Type": "application/json",
    },
    body: sessionId === undefined ? undefined : JSON.stringify({ sessionId }),
  });
}

async function pending() {
  const w = await seedMachineWorld(modules);
  await w.t.run(async (ctx) => {
    const assessment = await latestAssessment(
      ctx,
      w.patient,
      w.organizationId,
    );
    if (assessment === null) throw new Error("Missing test fitness");
    await ctx.db.patch(assessment._id, { validUntil: NOW + 60_000 });
  });
  const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
    machineId: w.machine,
    profileId: w.profileId,
    userId: w.patient,
  });
  expect(
    await w.t.query(internal.training.getPendingTrainingSession, {
      machineId: w.machine,
    }),
  ).toMatchObject({ sessionId });
  vi.setSystemTime(NOW + 1000);
  return { w, sessionId };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

describe("ANH-143 fitness and consent rechecked at arming", () => {
  it.each(changes)(
    "refuses at arming when $name: poll, 409, failed session and repeated confirmation",
    async ({ code, change }) => {
      const { w, sessionId } = await pending();
      const machineBefore = await w.t.run((ctx) => ctx.db.get(w.machine));
      await change(w);

      const poll = await request(w, "/api/machine/training/poll");
      expect(poll.status).toBe(200);
      expect(await poll.json()).toMatchObject({ session: null });
      const start = await request(w, "/api/machine/training/start", sessionId);
      expect(start.status).toBe(409);
      const refusal = await start.json();
      expect(refusal).toMatchObject({ error: code, message: expect.any(String) });
      expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
        status: "failed",
        endCode: code,
        endedAt: Date.now(),
      });
      const repeated = await request(w, "/api/machine/training/start", sessionId);
      expect(repeated.status).toBe(409);
      expect(await repeated.json()).toEqual(refusal);
      expect(await w.t.run((ctx) => ctx.db.get(w.machine))).toEqual(machineBefore);

      vi.runOnlyPendingTimers();
      await w.t.finishInProgressScheduledFunctions();
      const alerts = await w.t.run((ctx) => ctx.db.query("notifications").collect());
      expect(alerts).toHaveLength(1);
      expect(alerts[0]).toMatchObject({
        userId: w.adminId,
        type: "session_failed",
        sessionId,
        count: 1,
      });
      expect(alerts[0].detail).toBeUndefined();
    },
  );

  it.each(changes)("the minute sweep fails the launch when $name", async ({ code, change }) => {
    const { w, sessionId } = await pending();
    await change(w);
    expect(await w.t.mutation(internal.training.sweepPendingSessions, {})).toEqual({
      failed: 1,
    });
    expect(await w.t.run((ctx) => ctx.db.get(sessionId))).toMatchObject({
      status: "failed",
      endCode: code,
      endedAt: Date.now(),
    });
    const repeated = await request(w, "/api/machine/training/start", sessionId);
    expect(repeated.status).toBe(409);
    expect(await repeated.json()).toMatchObject({ error: code });
    expect(await w.t.mutation(internal.training.sweepPendingSessions, {})).toEqual({
      failed: 0,
    });
    vi.runOnlyPendingTimers();
    await w.t.finishInProgressScheduledFunctions();
    const alerts = await w.t.run((ctx) => ctx.db.query("notifications").collect());
    expect(alerts).toHaveLength(1);
    expect(alerts[0]).toMatchObject({ sessionId, type: "session_failed", count: 1 });
    expect(alerts[0].detail).toBeUndefined();
  });
});
