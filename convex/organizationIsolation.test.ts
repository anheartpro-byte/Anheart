/// <reference types="vite/client" />
/**
 * ANH-114 EX-5 / EX-6: what the role x resource matrix cannot express about
 * the separation of two organisations.
 *
 * - both directions: the matrix calls from centre B on centre A; here centre A
 *   calls on centre B, by direct identifier;
 * - calls that MIX organisations in their arguments (a machine of one, an
 *   account of the other), including for the Anheart admin;
 * - an account that belongs to two organisations;
 * - what a machine writes through the machine routes;
 * - rows that have no organisation yet.
 */
import { afterEach, describe, expect, it } from "vitest";
import { api, internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { authorizedMachineLive } from "./lib/trainingPrivacy";
import type { CurrentUser } from "./lib/auth";
import {
  ANHEART_CLERK_ORG,
  ORG_A_CLERK_ORG,
  ORG_B_CLERK_ORG,
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedMachineWorld,
  seedWorld,
  type Actor,
  type World,
} from "./test.setup";

afterEach(() => {
  configureAnheartOrganization(null);
});

const LIVE = {
  runMode: "seance",
  phase: "hold",
  bpm: 137,
  motorRpm: 1200,
  outputRpm: 24,
  setpointMotorRpm: 1200,
  gLoad: 1.2,
  safetyAction: "none",
  updatedAt: NOW,
};

/** Centre B with a live session, telemetry, an ECG batch and a summary. */
async function withCentreBSession(w: World) {
  const sessionId = await addSession(w, {
    machineId: w.orgBMachine,
    userId: w.orgBPatient,
    status: "active",
    kind: "auto",
  });
  await w.t.run(async (ctx) => {
    await ctx.db.patch(w.orgBMachine, { live: { ...LIVE, sessionId } });
    await ctx.db.insert("training_telemetry", {
      organizationId: w.orgB,
      sessionId,
      machineId: w.orgBMachine,
      t: NOW,
      elapsedS: 1,
      phase: "hold",
      bpm: 137,
      motorRpm: 1200,
      outputRpm: 24,
      setpointMotorRpm: 1200,
      gLoad: 1.2,
      safetyAction: "none",
    });
    await ctx.db.insert("ecg_data", {
      sessionId,
      timestamp: Date.now() - 2000,
      sampleRate: 250,
      samples: [{ channel: "ECG", values: [1, 2, 3], unit: "mV" }],
    });
    await ctx.db.insert("session_summaries", {
      sessionId,
      duration: 600,
      metrics: { avgHeartRate: 140, minHeartRate: 120, maxHeartRate: 160 },
      downsampledEcg: [{ timestamp: NOW, value: 1 }],
      createdAt: NOW,
    });
    await ctx.db.insert("machine_heartbeats", {
      machineId: w.orgBMachine,
      timestamp: NOW,
    });
  });
  return sessionId;
}

function isEmpty(result: unknown) {
  return result === null || (Array.isArray(result) && result.length === 0);
}

// ---------------------------------------------------------------------------
// Both directions
// ---------------------------------------------------------------------------

describe("EX-6 a member of centre A never reads centre B, even by direct identifier", () => {
  const readers: Actor[] = ["orgAdmin", "manager", "patient"];

  it.each(readers)("%s reads nothing of centre B", async (actor) => {
    const w = await seedWorld(modules);
    const sessionId = await withCentreBSession(w);
    const me = as(w.t, actor);
    const machineId = w.orgBMachine;

    const reads: Record<string, unknown> = {
      "machines.getMachine": await me.query(api.machines.getMachine, {
        machineId,
      }),
      "machines.getRecentHeartbeats": await me.query(
        api.machines.getRecentHeartbeats,
        { machineId },
      ),
      "machines.getGestionnairesForMachine": await me.query(
        api.machines.getGestionnairesForMachine,
        { machineId },
      ),
      "machines.getMachinesForGestionnaire": await me.query(
        api.machines.getMachinesForGestionnaire,
        { gestionnaireId: w.orgBManager },
      ),
      "users.getUserById": await me.query(api.users.getUserById, {
        userId: w.orgBPatient,
      }),
      "users.getGestionnairesForPatient": await me.query(
        api.users.getGestionnairesForPatient,
        { userId: w.orgBPatient },
      ),
      "users.getPatientsForGestionnaire": await me.query(
        api.users.getPatientsForGestionnaire,
        { gestionnaireId: w.orgBManager },
      ),
      "sessions.getSession": await me.query(api.sessions.getSession, {
        sessionId,
      }),
      "sessions.getActiveSessionForMachine": await me.query(
        api.sessions.getActiveSessionForMachine,
        { machineId },
      ),
      "sessions.listSessions(machine)": await me.query(
        api.sessions.listSessions,
        {
          machineId,
        },
      ),
      "sessions.listSessions(user)": await me.query(api.sessions.listSessions, {
        userId: w.orgBPatient,
      }),
      "training.listLaunchRights": await me.query(
        api.training.listLaunchRights,
        {
          machineId,
        },
      ),
      "training.listMachineProfiles": await me.query(
        api.training.listMachineProfiles,
        { machineId },
      ),
      "training.getMachineLive": await me.query(api.training.getMachineLive, {
        machineId,
      }),
      "training.getSessionTelemetry": await me.query(
        api.training.getSessionTelemetry,
        { sessionId },
      ),
      "training.getTrainingSession": await me.query(
        api.training.getTrainingSession,
        { sessionId },
      ),
      "ecgData.getSessionAllData": await me.query(
        api.ecgData.getSessionAllData,
        {
          sessionId,
        },
      ),
      "ecgData.getRecentEcgData": await me.query(api.ecgData.getRecentEcgData, {
        sessionId,
      }),
      "ecgData.getSessionEcgRange": await me.query(
        api.ecgData.getSessionEcgRange,
        {
          sessionId,
          startTime: 0,
          endTime: Date.now() + 60_000,
        },
      ),
      "ecgData.getSessionDataStats": await me.query(
        api.ecgData.getSessionDataStats,
        { sessionId },
      ),
      "ecgData.getLatestEcgBatch": await me.query(
        api.ecgData.getLatestEcgBatch,
        {
          sessionId,
        },
      ),
      "sessionSummaries.getSummary": await me.query(
        api.sessionSummaries.getSummary,
        { sessionId },
      ),
      "sessionSummaries.getSummaryWithEcg": await me.query(
        api.sessionSummaries.getSummaryWithEcg,
        { sessionId },
      ),
    };

    const leaked = Object.entries(reads)
      .filter(([, result]) => !isEmpty(result))
      .map(([name]) => name);
    expect(leaked).toEqual([]);
  });

  it.each(readers)("%s lists nothing of centre B", async (actor) => {
    const w = await seedWorld(modules);
    await withCentreBSession(w);
    const me = as(w.t, actor);
    const foreign = new Set<string>([
      w.orgBMachine,
      w.orgBAdmin,
      w.orgBManager,
      w.orgBPatient,
    ]);

    const listed = [
      ...(await me.query(api.machines.listMachines, {})),
      ...(await me.query(api.training.listLaunchableMachines, {})),
      ...(await me.query(api.users.listUsers, {})),
      ...(await me.query(api.users.listGestionnaires, {})),
    ].map((row) => String(row._id));
    const sessions = [
      ...(await me.query(api.sessions.listSessions, {})),
      ...(await me.query(api.sessions.getCompletedSessionsForUser, {})),
    ].map((row) => row.patientName);

    expect(listed.filter((id) => foreign.has(id))).toEqual([]);
    expect(sessions.filter((name) => name.startsWith("orgB"))).toEqual([]);
  });

  it.each(["orgAdmin", "manager"] as const)(
    "%s changes nothing in centre B",
    async (actor) => {
      const w = await seedWorld(modules);
      const sessionId = await withCentreBSession(w);
      const pending = await addSession(w, {
        machineId: w.orgBMachine,
        userId: w.orgBPatient,
        status: "pending",
        kind: "recording",
      });
      const me = as(w.t, actor);
      const machineId = w.orgBMachine;
      const before = await snapshotCentreB(w);

      const attempts: Array<Promise<unknown>> = [
        me.mutation(api.machines.updateMachine, { machineId, name: "taken" }),
        me.mutation(api.machines.deleteMachine, { machineId }),
        me.mutation(api.machines.regenerateApiKey, { machineId }),
        me.mutation(api.machines.assignGestionnaireToMachine, {
          machineId,
          gestionnaireId: w.manager,
        }),
        me.mutation(api.machines.removeGestionnaireFromMachine, {
          machineId,
          gestionnaireId: w.orgBManager,
        }),
        me.mutation(api.users.updatePatient, {
          userId: w.orgBPatient,
          firstName: "taken",
        }),
        me.mutation(api.users.deleteUser, { userId: w.orgBPatient }),
        me.mutation(api.users.assignGestionnaireToUser, {
          userId: w.orgBPatient,
          gestionnaireId: w.manager,
        }),
        me.mutation(api.users.removeGestionnaireFromUser, {
          userId: w.orgBPatient,
          gestionnaireId: w.orgBManager,
        }),
        me.mutation(api.training.setUserPhysiology, {
          userId: w.orgBPatient,
          hrMax: 150,
        }),
        me.mutation(api.training.grantLaunchRight, {
          machineId,
          userId: w.patient,
        }),
        me.mutation(api.training.revokeLaunchRight, {
          machineId,
          userId: w.orgBPatient,
        }),
        me.mutation(api.training.launchAutoSession, {
          machineId,
          profileId: w.profileId,
          userId: w.patient,
        }),
        me.mutation(api.training.requestStop, { sessionId }),
        me.mutation(api.sessions.createSession, {
          machineId,
          userId: w.patient,
          channels: ["ECG"],
        }),
        me.mutation(api.sessions.endSession, { sessionId }),
        me.mutation(api.sessions.cancelSession, { sessionId: pending }),
      ];
      const outcomes = await Promise.allSettled(attempts);

      expect(outcomes.map((o) => o.status)).toEqual(
        attempts.map(() => "rejected"),
      );
      expect(await snapshotCentreB(w)).toEqual(before);
    },
  );
});

/** Everything of centre B a foreign write could have changed. */
async function snapshotCentreB(w: World) {
  return await w.t.run(async (ctx) => {
    const machine = await ctx.db.get(w.orgBMachine);
    const patient = await ctx.db.get(w.orgBPatient);
    const inB = <T extends { organizationId?: Id<"organizations"> }>(
      rows: T[],
    ) => rows.filter((row) => row.organizationId === w.orgB);
    return {
      machine: machine && {
        name: machine.name,
        apiKey: machine.apiKey,
        isDeleted: machine.isDeleted ?? false,
        status: machine.status,
      },
      patient: patient && {
        firstName: patient.firstName,
        hrMax: patient.hrMax ?? null,
      },
      memberships: inB(await ctx.db.query("memberships").collect()).length,
      machineLinks: inB(await ctx.db.query("machine_gestionnaires").collect())
        .length,
      patientLinks: inB(await ctx.db.query("user_gestionnaires").collect())
        .length,
      launchRights: inB(
        await ctx.db.query("machine_user_permissions").collect(),
      ).length,
      sessions: inB(await ctx.db.query("sessions").collect()).map((s) => ({
        status: s.status,
        stopRequested: s.stopRequestedAt !== undefined,
      })),
    };
  });
}

// ---------------------------------------------------------------------------
// Another organisation's resource answers like a resource that does not exist
// ---------------------------------------------------------------------------

type Targets = {
  machineId: Id<"machines">;
  userId: Id<"users">;
  sessionId: Id<"sessions">;
  pendingSessionId: Id<"sessions">;
};

/** Identifiers of rows that existed and were deleted: valid, and unknown. */
async function deletedTargets(w: World): Promise<Targets> {
  return await w.t.run(async (ctx) => {
    const machineId = await ctx.db.insert("machines", {
      name: "Removed machine",
      apiKey: "synthetic-hash",
      status: "online" as const,
      lastHeartbeat: NOW,
      config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
      createdAt: NOW,
    });
    const userId = await ctx.db.insert("users", {
      clerkId: "removed-account",
      role: "user" as const,
      firstName: "Removed",
      lastName: "Account",
      email: "removed-account@example.invalid",
      language: "fr" as const,
      createdAt: NOW,
    });
    const session = {
      machineId,
      status: "active" as const,
      startedAt: NOW,
      channels: ["ECG"],
    };
    const sessionId = await ctx.db.insert("sessions", session);
    const pendingSessionId = await ctx.db.insert("sessions", session);
    for (const id of [machineId, userId, sessionId, pendingSessionId]) {
      await ctx.db.delete(id);
    }
    return { machineId, userId, sessionId, pendingSessionId };
  });
}

/** The answer of a call: its result, or the message of its refusal. */
async function answerOf(call: Promise<unknown>) {
  return await call.then(
    (result) => ({ result }),
    (error: Error) => ({ refused: error.message }),
  );
}

describe("EX-5 a resource of another organisation answers like one that does not exist", () => {
  it.each(["orgAdmin", "manager", "patient"] as const)(
    "gives %s the same answer for centre B's identifiers and for unknown ones",
    async (actor) => {
      const w = await seedWorld(modules);
      const foreign: Targets = {
        machineId: w.orgBMachine,
        userId: w.orgBPatient,
        sessionId: await withCentreBSession(w),
        pendingSessionId: await addSession(w, {
          machineId: w.orgBMachine,
          userId: w.orgBPatient,
          status: "pending",
          kind: "recording",
        }),
      };
      const unknown = await deletedTargets(w);
      const me = as(w.t, actor);

      /** Every call that takes an identifier, aimed at one set of targets. */
      const calls = (t: Targets): Record<string, () => Promise<unknown>> => ({
        // --- writes
        "machines.updateMachine": () =>
          me.mutation(api.machines.updateMachine, {
            machineId: t.machineId,
            name: "taken",
          }),
        "machines.deleteMachine": () =>
          me.mutation(api.machines.deleteMachine, { machineId: t.machineId }),
        "machines.regenerateApiKey": () =>
          me.mutation(api.machines.regenerateApiKey, {
            machineId: t.machineId,
          }),
        "machines.restoreMachine": () =>
          me.mutation(api.machines.restoreMachine, { machineId: t.machineId }),
        "machines.assignMachineToGestionnaires": () =>
          me.mutation(api.machines.assignMachineToGestionnaires, {
            machineId: t.machineId,
            gestionnaireIds: [w.manager],
          }),
        "machines.assignGestionnaireToMachine": () =>
          me.mutation(api.machines.assignGestionnaireToMachine, {
            machineId: t.machineId,
            gestionnaireId: w.manager,
          }),
        "machines.assignGestionnaireToMachine (gestionnaire)": () =>
          me.mutation(api.machines.assignGestionnaireToMachine, {
            machineId: w.machine,
            gestionnaireId: t.userId,
          }),
        "machines.removeGestionnaireFromMachine": () =>
          me.mutation(api.machines.removeGestionnaireFromMachine, {
            machineId: t.machineId,
            gestionnaireId: w.manager,
          }),
        "users.updateUserRole": () =>
          me.mutation(api.users.updateUserRole, {
            userId: t.userId,
            role: "admin",
          }),
        "users.updatePatient": () =>
          me.mutation(api.users.updatePatient, {
            userId: t.userId,
            firstName: "taken",
          }),
        "users.deleteUser": () =>
          me.mutation(api.users.deleteUser, { userId: t.userId }),
        "users.assignGestionnaireToUser": () =>
          me.mutation(api.users.assignGestionnaireToUser, {
            userId: t.userId,
            gestionnaireId: w.manager,
          }),
        "users.assignGestionnaireToUser (gestionnaire)": () =>
          me.mutation(api.users.assignGestionnaireToUser, {
            userId: w.stranger,
            gestionnaireId: t.userId,
          }),
        "users.removeGestionnaireFromUser": () =>
          me.mutation(api.users.removeGestionnaireFromUser, {
            userId: t.userId,
            gestionnaireId: w.manager,
          }),
        "users.assignPatientsToGestionnaire": () =>
          me.mutation(api.users.assignPatientsToGestionnaire, {
            gestionnaireId: t.userId,
            patientIds: [],
          }),
        "training.setUserPhysiology": () =>
          me.mutation(api.training.setUserPhysiology, {
            userId: t.userId,
            hrMax: 150,
          }),
        "training.grantLaunchRight": () =>
          me.mutation(api.training.grantLaunchRight, {
            machineId: t.machineId,
            userId: w.patient,
          }),
        "training.grantLaunchRight (user)": () =>
          me.mutation(api.training.grantLaunchRight, {
            machineId: w.machine,
            userId: t.userId,
          }),
        "training.revokeLaunchRight": () =>
          me.mutation(api.training.revokeLaunchRight, {
            machineId: t.machineId,
            userId: w.patient,
          }),
        "training.launchAutoSession": () =>
          me.mutation(api.training.launchAutoSession, {
            machineId: t.machineId,
            profileId: w.profileId,
          }),
        "training.launchAutoSession (rider)": () =>
          me.mutation(api.training.launchAutoSession, {
            machineId: w.machine,
            profileId: w.profileId,
            userId: t.userId,
          }),
        "training.requestStop": () =>
          me.mutation(api.training.requestStop, { sessionId: t.sessionId }),
        "sessions.createSession": () =>
          me.mutation(api.sessions.createSession, {
            machineId: t.machineId,
            userId: w.patient,
            channels: ["ECG"],
          }),
        "sessions.createSession (patient)": () =>
          me.mutation(api.sessions.createSession, {
            machineId: w.machine,
            userId: t.userId,
            channels: ["ECG"],
          }),
        "sessions.endSession": () =>
          me.mutation(api.sessions.endSession, { sessionId: t.sessionId }),
        "sessions.cancelSession": () =>
          me.mutation(api.sessions.cancelSession, {
            sessionId: t.pendingSessionId,
          }),
        // --- reads
        "machines.getMachine": () =>
          me.query(api.machines.getMachine, { machineId: t.machineId }),
        "machines.getRecentHeartbeats": () =>
          me.query(api.machines.getRecentHeartbeats, {
            machineId: t.machineId,
          }),
        "machines.getGestionnairesForMachine": () =>
          me.query(api.machines.getGestionnairesForMachine, {
            machineId: t.machineId,
          }),
        "machines.getMachinesForGestionnaire": () =>
          me.query(api.machines.getMachinesForGestionnaire, {
            gestionnaireId: t.userId,
          }),
        "users.getUserById": () =>
          me.query(api.users.getUserById, { userId: t.userId }),
        "users.listUsers": () =>
          me.query(api.users.listUsers, { gestionnaireId: t.userId }),
        "users.getGestionnairesForPatient": () =>
          me.query(api.users.getGestionnairesForPatient, { userId: t.userId }),
        "users.getPatientsForGestionnaire": () =>
          me.query(api.users.getPatientsForGestionnaire, {
            gestionnaireId: t.userId,
          }),
        "sessions.getSession": () =>
          me.query(api.sessions.getSession, { sessionId: t.sessionId }),
        "sessions.getActiveSessionForMachine": () =>
          me.query(api.sessions.getActiveSessionForMachine, {
            machineId: t.machineId,
          }),
        "sessions.listSessions (machine)": () =>
          me.query(api.sessions.listSessions, { machineId: t.machineId }),
        "sessions.listSessions (user)": () =>
          me.query(api.sessions.listSessions, { userId: t.userId }),
        "training.listLaunchRights": () =>
          me.query(api.training.listLaunchRights, { machineId: t.machineId }),
        "training.listMachineProfiles": () =>
          me.query(api.training.listMachineProfiles, {
            machineId: t.machineId,
          }),
        "training.getMachineLive": () =>
          me.query(api.training.getMachineLive, { machineId: t.machineId }),
        "training.getSessionTelemetry": () =>
          me.query(api.training.getSessionTelemetry, {
            sessionId: t.sessionId,
          }),
        "training.getTrainingSession": () =>
          me.query(api.training.getTrainingSession, {
            sessionId: t.sessionId,
          }),
        "ecgData.getSessionAllData": () =>
          me.query(api.ecgData.getSessionAllData, { sessionId: t.sessionId }),
        "ecgData.getRecentEcgData": () =>
          me.query(api.ecgData.getRecentEcgData, { sessionId: t.sessionId }),
        "ecgData.getSessionEcgRange": () =>
          me.query(api.ecgData.getSessionEcgRange, {
            sessionId: t.sessionId,
            startTime: 0,
            endTime: NOW * 2,
          }),
        "ecgData.getSessionDataStats": () =>
          me.query(api.ecgData.getSessionDataStats, { sessionId: t.sessionId }),
        "ecgData.getLatestEcgBatch": () =>
          me.query(api.ecgData.getLatestEcgBatch, { sessionId: t.sessionId }),
        "sessionSummaries.getSummary": () =>
          me.query(api.sessionSummaries.getSummary, { sessionId: t.sessionId }),
        "sessionSummaries.getSummaryWithEcg": () =>
          me.query(api.sessionSummaries.getSummaryWithEcg, {
            sessionId: t.sessionId,
          }),
      });

      const onForeign = calls(foreign);
      const onUnknown = calls(unknown);
      const different: string[] = [];
      for (const name of Object.keys(onForeign)) {
        const foreignAnswer = await answerOf(onForeign[name]());
        const unknownAnswer = await answerOf(onUnknown[name]());
        if (JSON.stringify(foreignAnswer) !== JSON.stringify(unknownAnswer)) {
          different.push(
            `${name}: ${JSON.stringify(foreignAnswer)} vs ${JSON.stringify(unknownAnswer)}`,
          );
        }
      }

      expect(different).toEqual([]);
    },
  );
});

// ---------------------------------------------------------------------------
// Arguments that mix two organisations
// ---------------------------------------------------------------------------

describe("EX-5 a machine and an account of two different organisations never combine", () => {
  it("refuses a launch right on a machine for an account of another organisation", async () => {
    const w = await seedWorld(modules);
    const args = { machineId: w.machine, userId: w.orgBPatient };

    // Even for the Anheart admin: the right goes to a user of the machine's
    // organisation, and nobody else is one.
    for (const actor of ["manager", "orgAdmin", "admin"] as const) {
      await expect(
        as(w.t, actor).mutation(api.training.grantLaunchRight, args),
      ).rejects.toThrow(/Launch rights are granted to users/);
    }
    const rights = await w.t.run((ctx) =>
      ctx.db
        .query("machine_user_permissions")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .collect(),
    );
    expect(rights.map((r) => r.userId)).toEqual([w.patient]);
  });

  it("refuses a launch right on a machine that does not exist", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.otherMachine));

    await expect(
      as(w.t, "admin").mutation(api.training.grantLaunchRight, {
        machineId: w.otherMachine,
        userId: w.otherPatient,
      }),
    ).rejects.toThrow(/Machine not found/);
    await expect(
      as(w.t, "admin").mutation(api.machines.assignGestionnaireToMachine, {
        machineId: w.otherMachine,
        gestionnaireId: w.manager,
      }),
    ).rejects.toThrow(/Machine not found/);
    await expect(
      as(w.t, "admin").mutation(api.sessions.createSession, {
        machineId: w.otherMachine,
        userId: w.otherPatient,
        channels: ["ECG"],
      }),
    ).rejects.toThrow(/Machine not found/);
    // For anyone else it is a machine they have no access to, like a foreign one.
    await expect(
      as(w.t, "otherManager").mutation(api.machines.updateMachine, {
        machineId: w.otherMachine,
        name: "x",
      }),
    ).rejects.toThrow(/Not authorized to manage this machine/);
    await expect(
      as(w.t, "otherPatient").mutation(api.training.launchAutoSession, {
        machineId: w.otherMachine,
        profileId: w.profileId,
      }),
    ).rejects.toThrow(/have not been given the right/);
    expect(
      await as(w.t, "otherPatient").query(api.training.getMachineLive, {
        machineId: w.otherMachine,
      }),
    ).toBeNull();
    // And the machine routes' functions write and serve nothing for it.
    await expect(
      w.t.mutation(internal.training.syncProfiles, {
        machineId: w.otherMachine,
        storeRev: 2,
        programsEnabled: true,
        profiles: [],
      }),
    ).rejects.toThrow(/Machine not found/);
    expect(
      await w.t.query(internal.training.getRoster, {
        machineId: w.otherMachine,
      }),
    ).toEqual([]);
  });

  it("writes a launch right in the machine's organisation", async () => {
    const w = await seedWorld(modules);
    const newcomer = await as(w.t, "orgBManager").mutation(
      api.users.createPatient,
      {
        firstName: "New",
        lastName: "Rider",
        email: "new-rider@example.invalid",
      },
    );

    // The Anheart admin acts in centre B on centre B's machine and account.
    await as(w.t, "admin").mutation(api.training.grantLaunchRight, {
      machineId: w.orgBMachine,
      userId: newcomer,
    });

    const right = await w.t.run((ctx) =>
      ctx.db
        .query("machine_user_permissions")
        .withIndex("by_machine_and_user", (q) =>
          q.eq("machineId", w.orgBMachine).eq("userId", newcomer),
        )
        .unique(),
    );
    expect(right?.organizationId).toBe(w.orgB);
  });

  it("refuses a session on a machine for a rider of another organisation", async () => {
    const w = await seedWorld(modules);

    await expect(
      as(w.t, "admin").mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
        userId: w.orgBPatient,
      }),
    ).rejects.toThrow(/Not authorized to launch a session for this rider/);
    await expect(
      as(w.t, "admin").mutation(api.sessions.createSession, {
        machineId: w.machine,
        userId: w.orgBPatient,
        channels: ["ECG"],
      }),
    ).rejects.toThrow(/Not authorized to create session for this patient/);
    const sessions = await w.t.run((ctx) => ctx.db.query("sessions").collect());
    expect(sessions).toEqual([]);
  });

  it("stamps a launched session with its machine's organisation", async () => {
    const w = await seedWorld(modules);

    const byAdmin = await as(w.t, "admin").mutation(
      api.training.launchAutoSession,
      {
        machineId: w.orgBMachine,
        profileId: w.profileId,
        userId: w.orgBPatient,
      },
    );
    const byRecording = await as(w.t, "admin").mutation(
      api.sessions.createSession,
      { machineId: w.machine, userId: w.patient, channels: ["ECG"] },
    );

    const rows = await w.t.run(async (ctx) => ({
      launched: await ctx.db.get(byAdmin),
      recording: await ctx.db.get(byRecording),
    }));
    // The Anheart admin acts from the Anheart organisation: the session is
    // nevertheless the machine's, not the caller's.
    expect(rows.launched?.organizationId).toBe(w.orgB);
    expect(rows.recording?.organizationId).toBe(w.orgA);
  });

  it("only links a machine to gestionnaires of the machine's organisation", async () => {
    const w = await seedWorld(modules);
    const link = { machineId: w.machine, gestionnaireId: w.orgBManager };

    await expect(
      as(w.t, "orgAdmin").mutation(
        api.machines.assignGestionnaireToMachine,
        link,
      ),
    ).rejects.toThrow(/Target user is not a gestionnaire/);
    await expect(
      as(w.t, "admin").mutation(api.machines.assignGestionnaireToMachine, link),
    ).rejects.toThrow(/Target user is not a gestionnaire/);
    await as(w.t, "orgAdmin").mutation(
      api.machines.assignMachineToGestionnaires,
      {
        machineId: w.machine,
        gestionnaireIds: [w.otherManager, w.orgBManager, w.patient],
      },
    );

    const links = await w.t.run((ctx) =>
      ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .collect(),
    );
    expect(
      links.map((l) => ({ id: l.gestionnaireId, org: l.organizationId })),
    ).toEqual([{ id: w.otherManager, org: w.orgA }]);
  });

  it("creates a machine in the organisation the Anheart admin names, with that organisation's gestionnaires", async () => {
    const w = await seedWorld(modules);

    const created = await as(w.t, "admin").mutation(
      api.machines.createMachine,
      {
        name: "Machine for centre B",
        organizationId: w.orgB,
        gestionnaireIds: [w.manager, w.orgBManager],
      },
    );

    const { machine, links } = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(created.machineId),
      links: await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_machine", (q) => q.eq("machineId", created.machineId))
        .collect(),
    }));
    expect(machine?.organizationId).toBe(w.orgB);
    expect(
      links.map((l) => ({ id: l.gestionnaireId, org: l.organizationId })),
    ).toEqual([{ id: w.orgBManager, org: w.orgB }]);
    // Centre B sees it; centre A does not.
    const inB = await as(w.t, "orgBAdmin").query(api.machines.listMachines, {});
    const inA = await as(w.t, "orgAdmin").query(api.machines.listMachines, {});
    expect(inB.map((m) => m._id)).toContain(created.machineId);
    expect(inA.map((m) => m._id)).not.toContain(created.machineId);
  });

  it("refuses to create a machine in an organisation that does not exist", async () => {
    const w = await seedWorld(modules);
    const gone = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("organizations", {
        clerkOrgId: "org_synthetic_gone",
        name: "Gone",
        slug: "gone",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      });
      await ctx.db.delete(id);
      return id;
    });

    await expect(
      as(w.t, "admin").mutation(api.machines.createMachine, {
        name: "Nowhere",
        organizationId: gone,
      }),
    ).rejects.toThrow(/Organization not found/);
  });

  it("only links a patient to gestionnaires of the same organisation", async () => {
    const w = await seedWorld(modules);

    // A patient of A with a gestionnaire of B, and the reverse: refused to
    // everyone, the Anheart admin included.
    for (const actor of ["admin", "orgAdmin"] as const) {
      await expect(
        as(w.t, actor).mutation(api.users.assignGestionnaireToUser, {
          userId: w.stranger,
          gestionnaireId: w.orgBManager,
        }),
      ).rejects.toThrow(/Target user is not a gestionnaire/);
    }
    await expect(
      as(w.t, "admin").mutation(api.users.assignGestionnaireToUser, {
        userId: w.orgBPatient,
        gestionnaireId: w.manager,
      }),
    ).rejects.toThrow(/Target user is not a gestionnaire/);

    await as(w.t, "orgAdmin").mutation(api.users.assignPatientsToGestionnaire, {
      gestionnaireId: w.manager,
      patientIds: [w.stranger, w.stranger, w.orgBPatient, w.otherManager],
    });
    const created = await as(w.t, "orgAdmin").mutation(
      api.users.createPatient,
      {
        firstName: "New",
        lastName: "Patient",
        email: "new-patient@example.invalid",
        gestionnaireIds: [w.manager, w.orgBManager],
      },
    );

    const links = await w.t.run((ctx) =>
      ctx.db.query("user_gestionnaires").collect(),
    );
    const ofManager = links.filter((l) => l.gestionnaireId === w.manager);
    expect(
      ofManager.map((l) => ({ user: l.userId, org: l.organizationId })),
    ).toEqual([
      { user: w.stranger, org: w.orgA },
      { user: created, org: w.orgA },
    ]);
    // Centre B's links are exactly what they were.
    expect(
      links
        .filter((l) => l.organizationId === w.orgB)
        .map((l) => ({ user: l.userId, gestionnaire: l.gestionnaireId })),
    ).toEqual([{ user: w.orgBPatient, gestionnaire: w.orgBManager }]);
  });

  it("lets the Anheart admin link a patient in the patient's main organisation", async () => {
    const w = await seedWorld(modules);
    const second = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("users", {
        clerkId: "orgb-second-manager",
        role: "gestionnaire" as const,
        organizationId: w.orgB,
        firstName: "Second",
        lastName: "Manager",
        email: "orgb-second-manager@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      });
      await ctx.db.insert("memberships", {
        userId: id,
        organizationId: w.orgB,
        role: "gestionnaire",
        active: true,
      });
      return id;
    });

    await as(w.t, "admin").mutation(api.users.assignGestionnaireToUser, {
      userId: w.orgBPatient,
      gestionnaireId: second,
    });
    await as(w.t, "admin").mutation(api.users.assignPatientsToGestionnaire, {
      gestionnaireId: w.orgBManager,
      patientIds: [w.orgBPatient],
    });

    const links = await w.t.run((ctx) =>
      ctx.db
        .query("user_gestionnaires")
        .withIndex("by_user", (q) => q.eq("userId", w.orgBPatient))
        .collect(),
    );
    expect(links.map((l) => l.organizationId)).toEqual([w.orgB, w.orgB]);
    expect(links.map((l) => l.gestionnaireId).sort()).toEqual(
      [second, w.orgBManager].sort(),
    );
  });

  it("refuses a link for an account that has no main organisation", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.stranger, { organizationId: undefined });
      await ctx.db.patch(w.manager, { organizationId: undefined });
    });

    await expect(
      as(w.t, "admin").mutation(api.users.assignGestionnaireToUser, {
        userId: w.stranger,
        gestionnaireId: w.manager,
      }),
    ).rejects.toThrow(/Can only assign gestionnaires to patients/);
    await expect(
      as(w.t, "admin").mutation(api.users.assignPatientsToGestionnaire, {
        gestionnaireId: w.manager,
        patientIds: [w.patient],
      }),
    ).rejects.toThrow(/Target user is not a gestionnaire/);
  });

  it("names the account that holds an email only to its own organisation", async () => {
    const w = await seedWorld(modules);
    const create = (actor: Actor, email: string) =>
      as(w.t, actor).mutation(api.users.createPatient, {
        firstName: "Dup",
        lastName: "Licate",
        email,
      });

    // Same organisation: named, with the role held in that organisation.
    await expect(create("manager", "PATIENT@example.invalid")).rejects.toThrow(
      /Email already in use by an existing user account \(patient Synthetic\)/,
    );
    await expect(
      create("admin", "orgBPatient@example.invalid"),
    ).rejects.toThrow(
      /Email already in use by an existing user account \(orgBPatient Synthetic\)/,
    );
    // Another organisation: refused without a name or a role.
    for (const actor of ["manager", "orgAdmin"] as const) {
      const outcome = await create(actor, "orgBManager@example.invalid").then(
        () => "created",
        (error: Error) => error.message,
      );
      expect(outcome).toMatch(/Email already in use$/);
      expect(outcome).not.toMatch(/orgBManager Synthetic|gestionnaire/);
    }
  });
});

// ---------------------------------------------------------------------------
// An account in two organisations
// ---------------------------------------------------------------------------

/**
 * `manager` (gestionnaire of centre A) is also a gestionnaire of centre B, and
 * `shared` is a patient of both, linked to `manager` in both.
 */
async function withSharedAccounts(w: World) {
  return await w.t.run(async (ctx) => {
    const shared = await ctx.db.insert("users", {
      clerkId: "shared",
      role: "user" as const,
      organizationId: w.orgA,
      firstName: "Shared",
      lastName: "Patient",
      email: "shared@example.invalid",
      language: "fr" as const,
      createdAt: NOW,
      hrMax: 180,
      birthYear: 1990,
    });
    await ctx.db.insert("memberships", {
      userId: w.manager,
      organizationId: w.orgB,
      role: "gestionnaire",
      active: true,
    });
    for (const organizationId of [w.orgA, w.orgB]) {
      await ctx.db.insert("memberships", {
        userId: shared,
        organizationId,
        role: "user",
        active: true,
      });
      await ctx.db.insert("user_gestionnaires", {
        organizationId,
        userId: shared,
        gestionnaireId: w.manager,
        createdAt: NOW,
        createdBy: w.admin,
      });
    }
    for (const [organizationId, machineId] of [
      [w.orgA, w.machine],
      [w.orgB, w.orgBMachine],
    ] as const) {
      await ctx.db.insert("machine_user_permissions", {
        organizationId,
        machineId,
        userId: shared,
        grantedBy: w.manager,
        createdAt: NOW,
      });
    }
    return shared;
  });
}

const managerIn = (w: World, org_id: string) =>
  w.t.withIdentity({
    subject: "manager",
    org_id,
    org_role: "org:gestionnaire",
  });
const sharedIn = (w: World, org_id: string) =>
  w.t.withIdentity({ subject: "shared", org_id, org_role: "org:patient" });

describe("EX-5 an account of two organisations acts in one organisation at a time", () => {
  it("shows a gestionnaire only the patients of the organisation they act in", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);

    const inA = await managerIn(w, ORG_A_CLERK_ORG).query(
      api.users.listUsers,
      {},
    );
    const inB = await managerIn(w, ORG_B_CLERK_ORG).query(
      api.users.listUsers,
      {},
    );
    const patientsInB = await managerIn(w, ORG_B_CLERK_ORG).query(
      api.users.getPatientsForGestionnaire,
      {},
    );

    expect(inA.map((u) => u._id).sort()).toEqual([w.patient, shared].sort());
    expect(inB.map((u) => u._id)).toEqual([shared]);
    expect(patientsInB.map((u) => u._id)).toEqual([shared]);
    // Acting in B, their patient of A is out of reach, and so is A's machine.
    const asB = managerIn(w, ORG_B_CLERK_ORG);
    expect(
      await asB.query(api.users.getUserById, { userId: w.patient }),
    ).toBeNull();
    expect(
      await asB.query(api.machines.getMachine, { machineId: w.machine }),
    ).toBeNull();
    expect(await asB.query(api.machines.listMachines, {})).toEqual([]);
    await expect(
      asB.mutation(api.users.updatePatient, {
        userId: w.patient,
        firstName: "taken",
      }),
    ).rejects.toThrow(/Not authorized to edit this patient/);
  });

  it("counts a patient linked in two organisations once for the Anheart admin", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);
    const admin = as(w.t, "admin");

    const patients = await admin.query(api.users.getPatientsForGestionnaire, {
      gestionnaireId: w.manager,
    });
    const users = await admin.query(api.users.listUsers, {
      gestionnaireId: w.manager,
    });
    const gestionnaires = await admin.query(
      api.users.getGestionnairesForPatient,
      {
        userId: shared,
      },
    );
    const listed = await admin.query(api.users.listGestionnaires, {});

    expect(patients.map((u) => u._id).sort()).toEqual(
      [w.patient, shared].sort(),
    );
    expect(users.map((u) => u._id).sort()).toEqual([w.patient, shared].sort());
    expect(gestionnaires.map((g) => g._id)).toEqual([w.manager]);
    expect(listed.filter((g) => g._id === w.manager)).toHaveLength(1);
    // Across organisations: both links are counted for the Anheart admin...
    expect(listed.find((g) => g._id === w.manager)?.patientCount).toBe(3);
    // ... and only centre B's for centre B's admin.
    const inB = await as(w.t, "orgBAdmin").query(
      api.users.listGestionnaires,
      {},
    );
    expect(inB.find((g) => g._id === w.manager)).toMatchObject({
      patientCount: 1,
      machineCount: 0,
    });
  });

  it("adds and removes a link in the organisation of the call only", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);
    const linksOf = () =>
      w.t.run(async (ctx) =>
        (
          await ctx.db
            .query("user_gestionnaires")
            .withIndex("by_user", (q) => q.eq("userId", shared))
            .collect()
        )
          .map((l) => String(l.organizationId))
          .sort(),
      );

    // Removing in B leaves the link of A.
    await managerIn(w, ORG_B_CLERK_ORG).mutation(
      api.users.removeGestionnaireFromUser,
      { userId: shared, gestionnaireId: w.manager },
    );
    expect(await linksOf()).toEqual([String(w.orgA)]);
    // The link that remains in A gives the gestionnaire nothing in B, where
    // the patient is still a member.
    const inB = managerIn(w, ORG_B_CLERK_ORG);
    expect(
      await inB.query(api.users.getUserById, { userId: shared }),
    ).toBeNull();
    expect(await inB.query(api.users.listUsers, {})).toEqual([]);
    await expect(
      inB.mutation(api.users.updatePatient, { userId: shared, firstName: "x" }),
    ).rejects.toThrow(/Not authorized to edit this patient/);
    expect(
      await managerIn(w, ORG_A_CLERK_ORG).query(api.users.getUserById, {
        userId: shared,
      }),
    ).toMatchObject({ _id: shared });
    // The link of A does not count as "already assigned" in B.
    await managerIn(w, ORG_B_CLERK_ORG).mutation(
      api.users.assignGestionnaireToUser,
      { userId: shared, gestionnaireId: w.manager },
    );
    expect(await linksOf()).toEqual([String(w.orgA), String(w.orgB)].sort());
    await expect(
      managerIn(w, ORG_B_CLERK_ORG).mutation(
        api.users.assignGestionnaireToUser,
        {
          userId: shared,
          gestionnaireId: w.manager,
        },
      ),
    ).rejects.toThrow(/already assigned/);
    // The Anheart admin removes the pair everywhere.
    await as(w.t, "admin").mutation(api.users.removeGestionnaireFromUser, {
      userId: shared,
      gestionnaireId: w.manager,
    });
    expect(await linksOf()).toEqual([]);
  });

  it("replaces a gestionnaire's patients in the organisation of the call only", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);

    await as(w.t, "orgBAdmin").mutation(
      api.users.assignPatientsToGestionnaire,
      {
        gestionnaireId: w.manager,
        patientIds: [],
      },
    );

    const links = await w.t.run((ctx) =>
      ctx.db
        .query("user_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", w.manager))
        .collect(),
    );
    expect(
      links.map((l) => ({ user: l.userId, org: l.organizationId })),
    ).toEqual([
      { user: w.patient, org: w.orgA },
      { user: shared, org: w.orgA },
    ]);
  });

  it("gives a launch right effect only in the organisation of its machine", async () => {
    const w = await seedWorld(modules);
    await withSharedAccounts(w);
    const launchable = async (org_id: string) =>
      (
        await sharedIn(w, org_id).query(api.training.listLaunchableMachines, {})
      ).map((m) => m._id);

    expect(await launchable(ORG_A_CLERK_ORG)).toEqual([w.machine]);
    expect(await launchable(ORG_B_CLERK_ORG)).toEqual([w.orgBMachine]);
    // Acting in B, the right held on A's machine opens nothing.
    const asB = sharedIn(w, ORG_B_CLERK_ORG);
    await expect(
      asB.mutation(api.training.launchAutoSession, {
        machineId: w.machine,
        profileId: w.profileId,
      }),
    ).rejects.toThrow(/have not been given the right/);
    expect(
      await asB.query(api.training.getMachineLive, { machineId: w.machine }),
    ).toBeNull();
    expect(
      await asB.query(api.training.listMachineProfiles, {
        machineId: w.machine,
      }),
    ).toEqual([]);
    // And their own session of A is not served while they act in B.
    const sessionOfA = await addSession(w, {
      machineId: w.machine,
      userId: (await w.t.run((ctx) =>
        ctx.db
          .query("users")
          .withIndex("by_clerk_id", (q) => q.eq("clerkId", "shared"))
          .unique(),
      ))!._id,
      status: "completed",
      kind: "auto",
    });
    expect(
      await asB.query(api.sessions.getSession, { sessionId: sessionOfA }),
    ).toBeNull();
    expect(await asB.query(api.sessions.listSessions, {})).toEqual([]);
    expect(
      await asB.query(api.sessions.getCompletedSessionsForUser, {}),
    ).toEqual([]);
    expect(
      (
        await sharedIn(w, ORG_A_CLERK_ORG).query(
          api.sessions.getCompletedSessionsForUser,
          {},
        )
      ).map((s) => s._id),
    ).toEqual([sessionOfA]);
  });
});

describe("EX-5 removing an account never reaches another organisation", () => {
  const stateOf = (w: World, userId: Id<"users">) =>
    w.t.run(async (ctx) => {
      const organizations = async (
        rows: Array<{ organizationId?: Id<"organizations"> }>,
      ) => rows.map((row) => String(row.organizationId)).sort();
      return {
        user: await ctx.db.get(userId),
        memberships: await organizations(
          await ctx.db
            .query("memberships")
            .withIndex("by_user", (q) => q.eq("userId", userId))
            .collect(),
        ),
        links: await organizations(
          await ctx.db
            .query("user_gestionnaires")
            .withIndex("by_user", (q) => q.eq("userId", userId))
            .collect(),
        ),
        rights: await organizations(
          await ctx.db
            .query("machine_user_permissions")
            .withIndex("by_user", (q) => q.eq("userId", userId))
            .collect(),
        ),
      };
    });

  it("removes a shared patient from the caller's organisation and keeps the rest", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);

    await as(w.t, "orgBAdmin").mutation(api.users.deleteUser, {
      userId: shared,
    });

    const afterB = await stateOf(w, shared);
    expect(afterB.user).toMatchObject({ organizationId: w.orgA, role: "user" });
    expect(afterB.memberships).toEqual([String(w.orgA)]);
    expect(afterB.links).toEqual([String(w.orgA)]);
    expect(afterB.rights).toEqual([String(w.orgA)]);
    // Centre B no longer reaches the account.
    expect(
      await as(w.t, "orgBAdmin").query(api.users.getUserById, {
        userId: shared,
      }),
    ).toBeNull();

    // Removed from its last organisation, the account itself goes.
    await managerIn(w, ORG_A_CLERK_ORG).mutation(api.users.deleteUser, {
      userId: shared,
    });
    const afterA = await stateOf(w, shared);
    expect(afterA).toEqual({
      user: null,
      memberships: [],
      links: [],
      rights: [],
    });
  });

  it("moves the main organisation of an account removed from it", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);

    await as(w.t, "orgAdmin").mutation(api.users.deleteUser, {
      userId: shared,
    });

    const after = await stateOf(w, shared);
    expect(after.user).toMatchObject({ organizationId: w.orgB, role: "user" });
    expect(after.memberships).toEqual([String(w.orgB)]);
  });

  it("removes a gestionnaire's links of the caller's organisation only", async () => {
    const w = await seedWorld(modules);
    await withSharedAccounts(w);

    await as(w.t, "orgBAdmin").mutation(api.users.deleteUser, {
      userId: w.manager,
    });

    const rows = await w.t.run(async (ctx) => ({
      user: await ctx.db.get(w.manager),
      patientLinks: await ctx.db
        .query("user_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", w.manager))
        .collect(),
      machineLinks: await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", w.manager))
        .collect(),
    }));
    expect(rows.user?.organizationId).toBe(w.orgA);
    expect(rows.patientLinks.every((l) => l.organizationId === w.orgA)).toBe(
      true,
    );
    expect(rows.patientLinks).toHaveLength(2);
    expect(rows.machineLinks.map((l) => l.machineId)).toEqual([w.machine]);
  });

  it("lets a gestionnaire remove patients only", async () => {
    const w = await seedWorld(modules);
    // `otherManager` becomes, wrongly, a "patient" link of `manager`.
    await w.t.run((ctx) =>
      ctx.db.insert("user_gestionnaires", {
        organizationId: w.orgA,
        userId: w.otherManager,
        gestionnaireId: w.manager,
        createdAt: NOW,
        createdBy: w.admin,
      }),
    );

    await expect(
      as(w.t, "manager").mutation(api.users.deleteUser, {
        userId: w.otherManager,
      }),
    ).rejects.toThrow(/Can only delete patient accounts/);
    await expect(
      as(w.t, "manager").mutation(api.users.deleteUser, { userId: w.manager }),
    ).rejects.toThrow(/Cannot delete your own account/);
  });

  it("lets the Anheart admin delete an account from every organisation", async () => {
    const w = await seedWorld(modules);
    const shared = await withSharedAccounts(w);

    await as(w.t, "admin").mutation(api.users.deleteUser, { userId: shared });

    expect(await stateOf(w, shared)).toEqual({
      user: null,
      memberships: [],
      links: [],
      rights: [],
    });
    const gone = await w.t.run(async (ctx) => {
      const id = await ctx.db.insert("users", {
        clerkId: "gone",
        role: "user" as const,
        firstName: "Gone",
        lastName: "Account",
        email: "gone@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      });
      await ctx.db.delete(id);
      return id;
    });
    await expect(
      as(w.t, "admin").mutation(api.users.deleteUser, { userId: gone }),
    ).rejects.toThrow(/User not found/);
    await expect(
      as(w.t, "admin").mutation(api.users.updatePatient, {
        userId: gone,
        firstName: "x",
      }),
    ).rejects.toThrow(/User not found/);
  });
});

// ---------------------------------------------------------------------------
// What a machine writes
// ---------------------------------------------------------------------------

describe("EX-5 what a machine writes inherits the machine's organisation", () => {
  const profile = {
    profileId: "p2",
    name: "Programme p2",
    totalDurationS: 600,
    zoneLowBpm: 130,
    zoneHighBpm: 150,
    hardMaxBpm: 170,
    criticalBpm: 185,
    subjectHrMax: 190,
    minRunRpm: 20,
    maxRpm: 1500,
  };
  const point = {
    t: NOW,
    elapsedS: 1,
    phase: "hold",
    bpm: 137,
    motorRpm: 1200,
    outputRpm: 24,
    setpointMotorRpm: 1200,
    gLoad: 1.2,
    safetyAction: "none",
  };

  it("stamps local sessions, telemetry and profiles with the machine's organisation", async () => {
    const w = await seedWorld(modules);

    const sessionId = await w.t.mutation(
      internal.training.registerLocalSession,
      {
        machineId: w.orgBMachine,
        localRef: "local-1",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Operator",
        userId: w.orgBPatient,
      },
    );
    await w.t.mutation(internal.training.storeTelemetry, {
      machineId: w.orgBMachine,
      sessionId,
      points: [point],
    });
    await w.t.mutation(internal.training.syncProfiles, {
      machineId: w.orgBMachine,
      storeRev: 2,
      programsEnabled: true,
      profiles: [profile],
    });

    const rows = await w.t.run(async (ctx) => ({
      session: await ctx.db.get(sessionId),
      telemetry: await ctx.db
        .query("training_telemetry")
        .withIndex("by_session_and_t", (q) => q.eq("sessionId", sessionId))
        .collect(),
      profiles: await ctx.db
        .query("machine_profiles")
        .withIndex("by_machine", (q) => q.eq("machineId", w.orgBMachine))
        .collect(),
    }));
    expect(rows.session).toMatchObject({
      organizationId: w.orgB,
      userId: w.orgBPatient,
    });
    expect(rows.telemetry.map((r) => r.organizationId)).toEqual([w.orgB]);
    expect(rows.profiles.map((r) => r.organizationId)).toEqual([w.orgB]);
    // Centre B reads it; centre A does not.
    expect(
      await as(w.t, "orgBManager").query(api.training.getSessionTelemetry, {
        sessionId,
      }),
    ).toHaveLength(1);
    expect(
      await as(w.t, "manager").query(api.training.getSessionTelemetry, {
        sessionId,
      }),
    ).toEqual([]);
  });

  it("records a local session without the rider when the rider is not of the machine's organisation", async () => {
    const w = await seedWorld(modules);
    const register = (localRef: string, userId?: string) =>
      w.t.mutation(internal.training.registerLocalSession, {
        machineId: w.machine,
        localRef,
        kind: "manual",
        startedAt: NOW,
        operatorName: "Operator",
        userId,
      });

    const foreign = await register("local-foreign", w.orgBPatient);
    const malformed = await register("local-malformed", "not-an-identifier");
    const own = await register("local-own", w.patient);
    const none = await register("local-none");

    const rows = await w.t.run(async (ctx) => ({
      foreign: await ctx.db.get(foreign),
      malformed: await ctx.db.get(malformed),
      own: await ctx.db.get(own),
      none: await ctx.db.get(none),
    }));
    // The session is always recorded, in the machine's organisation.
    for (const session of Object.values(rows)) {
      expect(session).toMatchObject({
        organizationId: w.orgA,
        status: "active",
      });
    }
    expect(rows.foreign?.userId).toBeUndefined();
    expect(rows.malformed?.userId).toBeUndefined();
    expect(rows.none?.userId).toBeUndefined();
    expect(rows.own?.userId).toBe(w.patient);
    // Centre B's patient does not find a session of centre A among theirs.
    expect(
      await as(w.t, "orgBPatient").query(api.sessions.listSessions, {}),
    ).toEqual([]);
  });

  it("serves the machine a roster limited to active members of its organisation", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      // A right written for an account of another organisation, and one for an
      // account that has since left this organisation.
      for (const userId of [w.orgBPatient, w.otherPatient]) {
        await ctx.db.insert("machine_user_permissions", {
          organizationId: w.orgA,
          machineId: w.machine,
          userId,
          grantedBy: w.manager,
          createdAt: NOW,
        });
      }
      const membership = await ctx.db
        .query("memberships")
        .withIndex("by_user", (q) => q.eq("userId", w.otherPatient))
        .unique();
      await ctx.db.patch(membership!._id, { active: false });
    });

    const roster = await w.t.query(internal.training.getRoster, {
      machineId: w.machine,
    });

    expect(roster.map((r) => r.userId)).toEqual([w.patient]);
  });

  it("keeps the same guarantees through the machine routes", async () => {
    const w = await seedMachineWorld(modules);
    const foreign = await w.t.run(async (ctx) => {
      const organizationId = await ctx.db.insert("organizations", {
        clerkOrgId: ORG_B_CLERK_ORG,
        name: "Centre B",
        slug: "centre-b",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      });
      const userId = await ctx.db.insert("users", {
        clerkId: "http-foreign",
        role: "user" as const,
        organizationId,
        firstName: "Foreign",
        lastName: "Rider",
        email: "http-foreign@example.invalid",
        language: "fr" as const,
        createdAt: NOW,
      });
      await ctx.db.insert("memberships", {
        userId,
        organizationId,
        role: "user",
        active: true,
      });
      await ctx.db.insert("machine_user_permissions", {
        organizationId: w.organizationId,
        machineId: w.machine,
        userId,
        grantedBy: w.adminId,
        createdAt: NOW,
      });
      return userId;
    });
    const call = (method: "GET" | "POST", path: string, body?: unknown) =>
      w.t.fetch(path, {
        method,
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });

    const roster = (await (
      await call("GET", "/api/machine/roster")
    ).json()) as {
      riders: Array<{ userId: string }>;
    };
    const local = (await (
      await call("POST", "/api/machine/training/local", {
        localRef: "route-local",
        kind: "manual",
        startedAt: NOW,
        operatorName: "Operator",
        userId: foreign,
      })
    ).json()) as { sessionId: Id<"sessions"> };
    await call("POST", "/api/machine/training/telemetry", {
      sessionId: local.sessionId,
      points: [point],
    });
    await call("POST", "/api/machine/profiles", {
      storeRev: 3,
      programsEnabled: true,
      profiles: [profile],
    });

    expect(roster.riders.map((r) => r.userId)).toEqual([w.patient]);
    const rows = await w.t.run(async (ctx) => ({
      session: await ctx.db.get(local.sessionId),
      telemetry: await ctx.db
        .query("training_telemetry")
        .withIndex("by_session_and_t", (q) =>
          q.eq("sessionId", local.sessionId),
        )
        .collect(),
      profiles: await ctx.db
        .query("machine_profiles")
        .withIndex("by_machine", (q) => q.eq("machineId", w.machine))
        .collect(),
    }));
    expect(rows.session?.organizationId).toBe(w.organizationId);
    expect(rows.session?.userId).toBeUndefined();
    expect(rows.telemetry.map((r) => r.organizationId)).toEqual([
      w.organizationId,
    ]);
    expect(rows.profiles.map((r) => r.organizationId)).toEqual([
      w.organizationId,
    ]);
  });
});

// ---------------------------------------------------------------------------
// Rows without an organisation, and the Anheart admin
// ---------------------------------------------------------------------------

describe("EX-5 a row that has no organisation is served to the Anheart admin only", () => {
  async function withUnattachedRows(w: World) {
    return await w.t.run(async (ctx) => {
      const machine = await ctx.db.insert("machines", {
        name: "Unattached machine",
        apiKey: "synthetic-hash",
        status: "online" as const,
        lastHeartbeat: NOW,
        config: { sampleRate: 1000, channels: ["ECG"], batchInterval: 1000 },
        createdAt: NOW,
        programsEnabled: true,
      });
      await ctx.db.insert("machine_gestionnaires", {
        machineId: machine,
        gestionnaireId: w.manager,
        isOwner: true,
        createdAt: NOW,
        createdBy: w.admin,
      });
      await ctx.db.insert("machine_user_permissions", {
        machineId: machine,
        userId: w.patient,
        grantedBy: w.manager,
        createdAt: NOW,
      });
      // A session on centre A's machine that lost its organisation.
      const session = await ctx.db.insert("sessions", {
        machineId: w.machine,
        userId: w.patient,
        status: "completed" as const,
        startedAt: NOW,
        channels: ["ECG"],
        kind: "auto" as const,
      });
      return { machine, session };
    });
  }

  it.each(["orgAdmin", "manager", "patient"] as const)(
    "hides it from %s, linked or not",
    async (actor) => {
      const w = await seedWorld(modules);
      const rows = await withUnattachedRows(w);
      const me = as(w.t, actor);

      expect(
        await me.query(api.machines.getMachine, { machineId: rows.machine }),
      ).toBeNull();
      expect(
        (await me.query(api.machines.listMachines, {})).map((m) => m._id),
      ).not.toContain(rows.machine);
      expect(
        (await me.query(api.training.listLaunchableMachines, {})).map(
          (m) => m._id,
        ),
      ).not.toContain(rows.machine);
      expect(
        await me.query(api.training.getMachineLive, {
          machineId: rows.machine,
        }),
      ).toBeNull();
      expect(
        await me.query(api.sessions.getSession, { sessionId: rows.session }),
      ).toBeNull();
      expect(await me.query(api.sessions.listSessions, {})).toEqual([]);
    },
  );

  it("refuses to launch on it, and to link it, until it has an organisation", async () => {
    const w = await seedWorld(modules);
    const rows = await withUnattachedRows(w);

    await expect(
      as(w.t, "patient").mutation(api.training.launchAutoSession, {
        machineId: rows.machine,
        profileId: w.profileId,
      }),
    ).rejects.toThrow(/have not been given the right/);
    await expect(
      as(w.t, "manager").mutation(api.machines.updateMachine, {
        machineId: rows.machine,
        name: "taken",
      }),
    ).rejects.toThrow(/Not authorized to manage this machine/);
    // The Anheart admin reaches the machine, but nobody is a member of "no
    // organisation": no gestionnaire and no rider can be attached to it.
    await expect(
      as(w.t, "admin").mutation(api.machines.assignGestionnaireToMachine, {
        machineId: rows.machine,
        gestionnaireId: w.otherManager,
      }),
    ).rejects.toThrow(/Target user is not a gestionnaire/);
    await expect(
      as(w.t, "admin").mutation(api.training.grantLaunchRight, {
        machineId: rows.machine,
        userId: w.stranger,
      }),
    ).rejects.toThrow(/Launch rights are granted to users/);
  });

  it("shows it to the Anheart admin", async () => {
    const w = await seedWorld(modules);
    const rows = await withUnattachedRows(w);
    const admin = as(w.t, "admin");

    expect(
      await admin.query(api.machines.getMachine, { machineId: rows.machine }),
    ).toMatchObject({ _id: rows.machine });
    expect(
      (await admin.query(api.machines.listMachines, {})).map((m) => m._id),
    ).toContain(rows.machine);
    expect(
      await admin.query(api.sessions.getSession, { sessionId: rows.session }),
    ).toMatchObject({ _id: rows.session });
  });
});

describe("EX-5 the Anheart admin reads and manages every organisation", () => {
  it("reaches centre B's machine, patient and session from the Anheart organisation", async () => {
    const w = await seedWorld(modules);
    const sessionId = await withCentreBSession(w);
    const admin = as(w.t, "admin");

    expect(
      await admin.query(api.machines.getMachine, { machineId: w.orgBMachine }),
    ).toMatchObject({ _id: w.orgBMachine });
    expect(
      await admin.query(api.users.getUserById, { userId: w.orgBPatient }),
    ).toMatchObject({ _id: w.orgBPatient, role: "user" });
    expect(
      await admin.query(api.sessions.getSession, { sessionId }),
    ).toMatchObject({ _id: sessionId });
    expect(
      await admin.query(api.training.getSessionTelemetry, { sessionId }),
    ).toHaveLength(1);
    expect(
      (
        await admin.query(api.training.getMachineLive, {
          machineId: w.orgBMachine,
        })
      )?.live,
    ).toMatchObject({ sessionId });
    await admin.mutation(api.machines.updateMachine, {
      machineId: w.orgBMachine,
      location: "Visited",
    });
    await admin.mutation(api.training.requestStop, { sessionId });
    const rows = await w.t.run(async (ctx) => ({
      machine: await ctx.db.get(w.orgBMachine),
      session: await ctx.db.get(sessionId),
    }));
    expect(rows.machine?.location).toBe("Visited");
    expect(rows.session?.stopRequestedAt).toBeDefined();
  });

  it("lists a gestionnaire's machines across organisations for the Anheart admin only", async () => {
    const w = await seedWorld(modules);
    await withSharedAccounts(w);
    await w.t.run((ctx) =>
      ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgB,
        machineId: w.orgBMachine,
        gestionnaireId: w.manager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      }),
    );
    const machinesOf = async (actor: Actor) =>
      (
        await as(w.t, actor).query(api.machines.getMachinesForGestionnaire, {
          gestionnaireId: w.manager,
        })
      )
        .map((m) => m._id)
        .sort();

    expect(await machinesOf("admin")).toEqual(
      [w.machine, w.orgBMachine].sort(),
    );
    expect(await machinesOf("orgAdmin")).toEqual([w.machine]);
    expect(await machinesOf("orgBAdmin")).toEqual([w.orgBMachine]);
    // The gestionnaire themselves: the machines of the organisation they act in.
    expect(
      (
        await managerIn(w, ORG_B_CLERK_ORG).query(api.machines.listMachines, {})
      ).map((m) => m._id),
    ).toEqual([w.orgBMachine]);
    expect(
      (
        await managerIn(w, ORG_B_CLERK_ORG).query(
          api.machines.getMachinesForGestionnaire,
          {},
        )
      ).map((m) => m._id),
    ).toEqual([w.orgBMachine]);
  });
});

describe("EX-5 a list is cut inside the caller's organisation", () => {
  it("never lets another organisation's sessions fill the caller's page", async () => {
    const w = await seedWorld(modules);
    const own = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "completed",
      kind: "auto",
    });
    // Newer sessions of centre B: with a page of one, they would come first.
    for (let i = 0; i < 3; i++) {
      await addSession(w, {
        machineId: w.orgBMachine,
        userId: w.orgBPatient,
        status: "completed",
        kind: "auto",
      });
    }

    for (const actor of ["orgAdmin", "manager", "patient"] as const) {
      const page = await as(w.t, actor).query(api.sessions.listSessions, {
        limit: 1,
      });
      expect(
        page.map((s) => s._id),
        actor,
      ).toEqual([own]);
    }
    // The Anheart admin's page is cut across organisations.
    const adminPage = await as(w.t, "admin").query(api.sessions.listSessions, {
      limit: 1,
    });
    expect(adminPage.map((s) => s.patientName)).toEqual([
      "orgBPatient Synthetic",
    ]);
  });
});

describe("EX-5 live measures never leave the machine's organisation", () => {
  it("returns no live state to a caller of another organisation, whatever their role there", async () => {
    const w = await seedWorld(modules);
    const sessionId = await withCentreBSession(w);
    const callerOf = (
      userId: Id<"users">,
      role: CurrentUser["role"],
      organizationId: Id<"organizations">,
    ) =>
      w.t.run(async (ctx) => {
        const user = (await ctx.db.get(userId))!;
        const machine = (await ctx.db.get(w.orgBMachine))!;
        const live = await authorizedMachineLive(
          ctx,
          { ...user, role, organizationId },
          machine,
        );
        return live?.sessionId ?? null;
      });

    // The rider of the live session, but acting in centre A.
    expect(await callerOf(w.orgBPatient, "user", w.orgA)).toBeNull();
    expect(await callerOf(w.orgAdmin, "org_admin", w.orgA)).toBeNull();
    // In the machine's organisation: its rider and its admin read it.
    expect(await callerOf(w.orgBPatient, "user", w.orgB)).toBe(sessionId);
    expect(await callerOf(w.orgBAdmin, "org_admin", w.orgB)).toBe(sessionId);
    expect(await callerOf(w.admin, "admin", w.anheartOrg)).toBe(sessionId);
  });
});

describe("the synthetic world itself", () => {
  it("declares the Anheart organisation the matrix relies on", async () => {
    const w = await seedWorld(modules);

    expect(process.env.ANHEART_ORG_ID).toBe(ANHEART_CLERK_ORG);
    const organizations = await w.t.run((ctx) =>
      ctx.db.query("organizations").collect(),
    );
    expect(organizations.map((o) => o.clerkOrgId).sort()).toEqual(
      [ANHEART_CLERK_ORG, ORG_A_CLERK_ORG, ORG_B_CLERK_ORG].sort(),
    );
  });
});
