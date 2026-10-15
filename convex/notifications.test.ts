/// <reference types="vite/client" />
/**
 * ANH-90: the alerts to the gestionnaires. Who receives one, when the server
 * writes one (machine offline, drive fault, failed session, safety verdict),
 * how a new event folds into an unread alert, what the site reads, and the
 * e-mail that may go with a new alert, which never carries health data.
 *
 * Every timer is fake: the e-mail component's batch, scheduled when an e-mail
 * is queued, never runs, and nothing is sent anywhere.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, components, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import {
  NOTIFICATION_RETENTION_MS,
  driveFaultAlerts,
  driveFaultCode,
  emailChoices,
  foldedDetail,
  notifyMachine,
  verdictAction,
  verdictAlerts,
} from "./lib/notifications";
import {
  alertEmail,
  emailSettings,
  mailableName,
} from "./lib/notificationEmail";
import { UNREAD_COUNT_LIMIT } from "./notifications";
import {
  addSession,
  as,
  configureAnheartOrganization,
  modules,
  NOW,
  seedLaunchWorld as seedWorld,
  type World,
} from "./test.setup";

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  configureAnheartOrganization(null);
});

/** The alerts of the store, as `[recipient, type, machine]`, sorted. */
async function alerts(w: World) {
  return await w.t.run((ctx) => ctx.db.query("notifications").collect());
}

function recipients(rows: Doc<"notifications">[]) {
  return rows.map((row) => row.userId).sort();
}

const live = (fields: Partial<Doc<"machines">["live"]> = {}) => ({
  runMode: "seance",
  phase: "hold",
  motorRpm: 1200,
  outputRpm: 24,
  setpointMotorRpm: 1200,
  gLoad: 1.2,
  safetyAction: "none",
  driveState: "operation_enabled",
  updatedAt: NOW,
  ...fields,
});

const event = (seq: number, kind: string, detail: string) => ({
  seq,
  t: NOW + seq,
  kind,
  detail,
  actor: "system",
});

/**
 * Run the alert deliveries scheduled so far (`notifications.deliver`), and
 * nothing they schedule in turn: the e-mail component's batch stays queued.
 */
async function delivered(w: World) {
  vi.runOnlyPendingTimers();
  await w.t.finishInProgressScheduledFunctions();
}

async function storeEvents(
  w: World,
  sessionId: Id<"sessions">,
  events: ReturnType<typeof event>[],
) {
  const outcome = await w.t.mutation(internal.training.storeEvents, {
    machineId: w.machine,
    sessionId,
    events,
  });
  await delivered(w);
  return outcome;
}

async function markOffline(w: World, machineId: Id<"machines">) {
  await w.t.run((ctx) =>
    ctx.db.patch(machineId, { status: "online", lastHeartbeat: NOW - 100_000 }),
  );
  await w.t.mutation(internal.machines.checkOfflineMachines, {});
  await delivered(w);
}

function setEmailVariables() {
  vi.stubEnv("RESEND_API_KEY", "re_synthetic_key");
  vi.stubEnv("NOTIFICATION_EMAIL_FROM", "Gaura <alertes@example.invalid>");
  vi.stubEnv("SITE_URL", "https://site.example.invalid/");
}

async function emailOf(w: World, emailId: string | undefined) {
  if (emailId === undefined) throw new Error("No e-mail queued");
  return await w.t.query(components.resend.lib.get, { emailId });
}

// ---------------------------------------------------------------------------
// Who receives an alert
// ---------------------------------------------------------------------------

describe("who receives an alert about a machine", () => {
  it("is its gestionnaire and its organisation's admin, nobody else", async () => {
    const w = await seedWorld(modules);

    await markOffline(w, w.machine);

    const rows = await alerts(w);
    expect(recipients(rows)).toEqual([w.orgAdmin, w.manager].sort());
    for (const row of rows) {
      expect(row).toMatchObject({
        organizationId: w.orgA,
        machineId: w.machine,
        type: "machine_offline",
        count: 1,
        read: false,
        createdAt: NOW,
        updatedAt: NOW,
      });
      expect(row.sessionId).toBeUndefined();
      expect(row.emailId).toBeUndefined();
    }
  });

  it("leaves out a gestionnaire no longer active there, or no longer a gestionnaire, and an account deleted in Clerk", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      // The other gestionnaire is linked to this machine too, then demoted.
      await ctx.db.insert("machine_gestionnaires", {
        organizationId: w.orgA,
        machineId: w.machine,
        gestionnaireId: w.otherManager,
        isOwner: false,
        createdAt: NOW,
        createdBy: w.admin,
      });
      const membershipOf = async (userId: Id<"users">) => {
        const row = await ctx.db
          .query("memberships")
          .withIndex("by_user_and_organization", (q) =>
            q.eq("userId", userId).eq("organizationId", w.orgA),
          )
          .unique();
        if (row === null) throw new Error("No membership");
        return row._id;
      };
      await ctx.db.patch(await membershipOf(w.otherManager), { role: "user" });
      await ctx.db.patch(await membershipOf(w.manager), { active: false });
      await ctx.db.patch(w.orgAdmin, { clerkDeletedAt: NOW });
    });

    await markOffline(w, w.machine);

    expect(await alerts(w)).toEqual([]);
  });

  it("is nobody for a deleted machine, a machine without organisation, or one whose organisation was deleted", async () => {
    const w = await seedWorld(modules);
    const written = await w.t.run(async (ctx) => {
      const machine = await ctx.db.get(w.machine);
      const other = await ctx.db.get(w.orgBMachine);
      if (machine === null || other === null) throw new Error("No machine");
      const counts = [
        await notifyMachine(ctx, { ...machine, isDeleted: true }, "drive_fault"),
        await notifyMachine(
          ctx,
          { ...machine, organizationId: undefined },
          "drive_fault",
        ),
      ];
      await ctx.db.patch(w.orgB, { deletedAt: NOW });
      counts.push(await notifyMachine(ctx, other, "drive_fault"));
      return counts;
    });

    expect(written).toEqual([0, 0, 0]);
    expect(await alerts(w)).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// When the server writes one
// ---------------------------------------------------------------------------

describe("a machine that goes offline", () => {
  it("is one alert per recipient while it is unread, and a new one after it was read", async () => {
    const w = await seedWorld(modules);

    await markOffline(w, w.machine);
    vi.setSystemTime(NOW + 60_000);
    await markOffline(w, w.machine);

    let rows = await alerts(w);
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => [row.count, row.updatedAt])).toEqual([
      [2, NOW + 60_000],
      [2, NOW + 60_000],
    ]);

    await as(w.t, "manager").mutation(api.notifications.markAllRead, {});
    await markOffline(w, w.machine);

    rows = await alerts(w);
    expect(rows).toHaveLength(3);
    const managers = rows.filter((row) => row.userId === w.manager);
    expect(managers.map((row) => [row.read, row.count])).toEqual([
      [true, 2],
      [false, 1],
    ]);
  });

  it("is not alerted again by a machine already offline", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);
    await as(w.t, "manager").mutation(api.notifications.markAllRead, {});

    // Still silent, already offline: the check does not mark it again.
    await w.t.mutation(internal.machines.checkOfflineMachines, {});

    const unread = (await alerts(w)).filter((row) => !row.read);
    expect(recipients(unread)).toEqual([w.orgAdmin]);
  });
});

describe("a drive in fault, as the heartbeat says", () => {
  it("alerts when the drive enters the fault, not while it stays in it", async () => {
    const w = await seedWorld(modules);
    const report = async (driveState: string) => {
      await w.t.mutation(internal.training.updateLive, {
        machineId: w.machine,
        live: live({ driveState }),
      });
      await delivered(w);
    };

    await report("operation_enabled");
    expect(await alerts(w)).toEqual([]);
    await report("fault");
    await report("fault");
    await report("fault");

    const rows = await alerts(w);
    expect(recipients(rows)).toEqual([w.orgAdmin, w.manager].sort());
    expect(rows.map((row) => [row.type, row.count])).toEqual([
      ["drive_fault", 1],
      ["drive_fault", 1],
    ]);

    // Reset, then a new fault: it folds into the unread alert.
    await report("ready");
    await report("fault");
    expect((await alerts(w)).map((row) => row.count)).toEqual([2, 2]);
  });

  it("alerts for a first state in fault, and names the session the machine runs when it is its own", async () => {
    const w = await seedWorld(modules);
    const own = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    await w.t.mutation(internal.training.updateLive, {
      machineId: w.machine,
      live: live({ driveState: "fault", sessionId: own }),
    });
    await delivered(w);

    expect((await alerts(w)).map((row) => row.sessionId)).toEqual([own, own]);
  });

  it("names no session that is not the machine's own, or that is no session", async () => {
    const w = await seedWorld(modules);
    const elsewhere = await addSession(w, {
      machineId: w.otherMachine,
      userId: w.otherPatient,
      status: "active",
      kind: "auto",
    });

    await w.t.mutation(internal.training.updateLive, {
      machineId: w.machine,
      live: live({ driveState: "fault", sessionId: elsewhere }),
    });
    await delivered(w);
    await w.t.mutation(internal.training.updateLive, {
      machineId: w.otherMachine,
      live: live({ driveState: "fault", sessionId: "not-a-session" }),
    });
    await delivered(w);

    const rows = await alerts(w);
    expect(rows).toHaveLength(4);
    expect(rows.every((row) => row.sessionId === undefined)).toBe(true);
  });

  it("refuses the live state of a machine whose row is gone, and alerts nobody", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    await expect(
      w.t.mutation(internal.training.updateLive, {
        machineId: w.machine,
        live: live({ driveState: "fault" }),
      }),
    ).rejects.toThrow(/machine_not_found/);
    expect(await alerts(w)).toEqual([]);
  });
});

describe("a session the machine ends as failed", () => {
  it("is an alert naming the session, once however many times the end is sent", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    const end = async () => {
      await w.t.mutation(internal.training.endTrainingSession, {
        machineId: w.machine,
        sessionId,
        failed: true,
        reason: "safety verdict: ramp down",
      });
      await delivered(w);
    };

    await end();
    await end();

    const rows = await alerts(w);
    expect(rows.map((row) => [row.type, row.sessionId, row.count])).toEqual([
      ["session_failed", sessionId, 1],
      ["session_failed", sessionId, 1],
    ]);
    // The reason stays on the session: the alert holds no sentence.
    expect(rows.every((row) => row.detail === undefined)).toBe(true);
  });

  it("is no alert for a session that completed", async () => {
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
      failed: false,
      reason: "programme complete",
    });
    await delivered(w);

    expect(await alerts(w)).toEqual([]);
  });

  it("is one alert per failed session, not one folded across sessions", async () => {
    const w = await seedWorld(modules);
    for (let i = 0; i < 2; i++) {
      const sessionId = await addSession(w, {
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      await w.t.mutation(internal.training.endTrainingSession, {
        machineId: w.machine,
        sessionId,
        failed: true,
        reason: "emergency stop",
      });
      await delivered(w);
    }

    const managers = (await alerts(w)).filter((row) => row.userId === w.manager);
    expect(managers).toHaveLength(2);
    expect(new Set(managers.map((row) => row.sessionId)).size).toBe(2);
  });
});

describe("a launch failed before its machine armed it (ANH-144)", () => {
  const launch = (w: World) =>
    as(w.t, "manager").mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      profileId: w.profileId,
      userId: w.patient,
    });
  const scheduled = (w: World) =>
    w.t.run(async (ctx) =>
      (await ctx.db.system.query("_scheduled_functions").collect())
        .filter((job) => job.state.kind === "pending")
        .map((job) => job.name),
    );

  it("is an alert once the sweep has failed it, written apart from the sweep", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w);
    vi.setSystemTime(NOW + 301_000);

    expect(
      await w.t.mutation(internal.training.sweepPendingSessions, {}),
    ).toEqual({ failed: 1 });
    // The sweep has done its work and written no alert: it only asked for one.
    expect((await w.t.run((ctx) => ctx.db.get(sessionId)))?.status).toBe(
      "failed",
    );
    expect(await alerts(w)).toEqual([]);
    expect(await scheduled(w)).toEqual(["notifications:deliver"]);

    await delivered(w);

    const rows = await alerts(w);
    expect(recipients(rows)).toEqual([w.orgAdmin, w.manager].sort());
    expect(rows.map((row) => [row.type, row.sessionId])).toEqual([
      ["session_failed", sessionId],
      ["session_failed", sessionId],
    ]);
    const [listed] = await as(w.t, "manager").query(api.notifications.list, {});
    expect(listed.reasonCode).toBe("expired_before_start");
  });

  it("is an alert when the start route refuses it, with its code and nothing of the rider", async () => {
    const w = await seedWorld(modules);
    setEmailVariables();
    const sessionId = await launch(w);
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 171 }));

    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toMatchObject({ armed: false, code: "hr_max_changed_before_start" });
    await delivered(w);

    const rows = await alerts(w);
    expect(rows.map((row) => row.type)).toEqual([
      "session_failed",
      "session_failed",
    ]);
    expect(rows.every((row) => row.detail === undefined)).toBe(true);
    const [listed] = await as(w.t, "manager").query(api.notifications.list, {});
    expect(listed.reasonCode).toBe("hr_max_changed_before_start");
    for (const row of rows) {
      const everything = JSON.stringify(await emailOf(w, row.emailId));
      for (const forbidden of ["171", "heart", "hr_max", "patient", sessionId]) {
        expect(everything).not.toContain(forbidden);
      }
    }
    // The same confirmation sent again reads the same, and alerts no more.
    await w.t.mutation(internal.training.markTrainingStarted, {
      machineId: w.machine,
      sessionId,
    });
    await delivered(w);
    expect((await alerts(w)).map((row) => row.count)).toEqual([1, 1]);
  });

  it("is an alert when a new launch fails the one that waited too long", async () => {
    const w = await seedWorld(modules);
    const first = await launch(w);
    vi.setSystemTime(NOW + 301_000);

    const second = await launch(w);
    await delivered(w);

    expect(second).not.toBe(first);
    expect((await alerts(w)).map((row) => row.sessionId)).toEqual([
      first,
      first,
    ]);
  });

  it("is no alert for a launch the machine armed", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w);

    expect(
      await w.t.mutation(internal.training.markTrainingStarted, {
        machineId: w.machine,
        sessionId,
      }),
    ).toEqual({ armed: true });
    await delivered(w);

    expect(await alerts(w)).toEqual([]);
  });
});

describe("the events of a session's record", () => {
  it("alert of a verdict that ends the session, with its action, and not of one that regulates", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    await storeEvents(w, sessionId, [
      event(1, "verdict", "hr_above_zone (REDUCE): 3 bpm over"),
      event(2, "verdict", "signal_stale (FREEZE): no fresh beat"),
      event(3, "verdict", "cleared"),
      event(4, "phase", "cooldown"),
    ]);
    expect(await alerts(w)).toEqual([]);

    await storeEvents(w, sessionId, [
      event(5, "verdict", "hr_critical (QUICK_STOP): 191 bpm"),
      event(6, "verdict", "hr_hard_max (RAMP_DOWN): 181 bpm"),
    ]);

    const rows = await alerts(w);
    expect(rows.map((row) => [row.type, row.sessionId, row.detail, row.count])).toEqual([
      ["safety_verdict", sessionId, "QUICK_STOP", 2],
      ["safety_verdict", sessionId, "QUICK_STOP", 2],
    ]);
    // Nothing the machine wrote after the action is kept.
    expect(JSON.stringify(rows)).not.toContain("bpm");
  });

  it("alert of an event sent again only once", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    const batch = [event(1, "verdict", "estop (GO_SILENT): pressed")];

    await storeEvents(w, sessionId, batch);
    expect(await storeEvents(w, sessionId, batch)).toEqual({
      stored: 0,
      duplicates: 1,
      rejected: 0,
    });

    expect((await alerts(w)).map((row) => row.count)).toEqual([1, 1]);
  });

  it("alert of a verdict whose action this server cannot read, without a code", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    await storeEvents(w, sessionId, [event(1, "verdict", "a verdict of another form")]);

    expect((await alerts(w)).map((row) => [row.type, row.detail])).toEqual([
      ["safety_verdict", undefined],
      ["safety_verdict", undefined],
    ]);
  });

  it("alert of a drive fault with its mnemonic, folded with the one the heartbeat reported", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    await w.t.mutation(internal.training.updateLive, {
      machineId: w.machine,
      live: live({ driveState: "fault" }),
    });
    await delivered(w);

    await storeEvents(w, sessionId, [
      event(1, "drive_fault", "OHF (LFT 17): drive overheat"),
      event(2, "drive_fault", "cleared"),
    ]);

    const rows = await alerts(w);
    expect(
      rows.map((row) => [row.type, row.detail, row.count, row.sessionId]),
    ).toEqual([
      ["drive_fault", "OHF", 2, sessionId],
      ["drive_fault", "OHF", 2, sessionId],
    ]);
  });

  it("alerts nobody for the events of a machine whose row is gone", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    await w.t.run((ctx) => ctx.db.delete(w.machine));

    expect(
      await storeEvents(w, sessionId, [
        event(1, "verdict", "estop (GO_SILENT): pressed"),
        event(2, "drive_fault", "OHF (LFT 17): drive overheat"),
      ]),
    ).toEqual({ stored: 2, duplicates: 0, rejected: 0 });
    expect(await alerts(w)).toEqual([]);
  });
});

describe("what the machine wrote, read as a code", () => {
  it("reads the action of a verdict and the mnemonic of a drive fault, and nothing else", () => {
    expect(verdictAction("hr_critical (QUICK_STOP): 191 bpm")).toBe("QUICK_STOP");
    expect(verdictAction("cleared")).toBeNull();
    expect(verdictAction("HR (QUICK_STOP)")).toBeNull();
    expect(verdictAlerts("cleared")).toBe(false);
    expect(verdictAlerts(" cleared ")).toBe(false);
    expect(verdictAlerts("x (REDUCE): y")).toBe(false);
    expect(verdictAlerts("x (FREEZE): y")).toBe(false);
    expect(verdictAlerts("x (RAMP_DOWN): y")).toBe(true);
    expect(verdictAlerts("x (SOMETHING_NEW): y")).toBe(true);

    expect(driveFaultCode("OHF (LFT 17): overheat")).toBe("OHF");
    expect(driveFaultCode("cleared")).toBeNull();
    expect(driveFaultCode("overheat")).toBeNull();
    expect(driveFaultAlerts("cleared")).toBe(false);
    expect(driveFaultAlerts("OHF (LFT 17): overheat")).toBe(true);
  });

  it("keeps the most severe action of a verdict, and the latest code of anything else", () => {
    expect(foldedDetail("safety_verdict", "RAMP_DOWN", "QUICK_STOP")).toBe("QUICK_STOP");
    expect(foldedDetail("safety_verdict", "GO_SILENT", "RAMP_DOWN")).toBe("GO_SILENT");
    expect(foldedDetail("safety_verdict", undefined, null)).toBeUndefined();
    expect(foldedDetail("safety_verdict", "GO_SILENT", "UNKNOWN_ACTION")).toBe(
      "UNKNOWN_ACTION",
    );
    expect(foldedDetail("drive_fault", "OHF", "OCF")).toBe("OCF");
    expect(foldedDetail("drive_fault", "OHF", null)).toBe("OHF");
  });

  it("keeps no code that is not one", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      const machine = await ctx.db.get(w.machine);
      if (machine === null) throw new Error("No machine");
      await notifyMachine(ctx, machine, "drive_fault", {
        detail: "overheat at 191 bpm",
      });
    });

    expect((await alerts(w)).map((row) => row.detail)).toEqual([
      undefined,
      undefined,
    ]);
  });
});

// ---------------------------------------------------------------------------
// What the site reads
// ---------------------------------------------------------------------------

describe("the alerts a caller reads", () => {
  it("lists the newest change first, with the reason of a failed session", async () => {
    const w = await seedWorld(modules);
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });
    await markOffline(w, w.machine);
    vi.setSystemTime(NOW + 1_000);
    await w.t.mutation(internal.training.endTrainingSession, {
      machineId: w.machine,
      sessionId,
      failed: true,
      reason: "emergency stop",
    });
    await delivered(w);

    const list = await as(w.t, "manager").query(api.notifications.list, {});

    expect(list.map((row) => [row.type, row.machineName, row.reason])).toEqual([
      ["session_failed", "Synthetic machine", "emergency stop"],
      ["machine_offline", "Synthetic machine", undefined],
    ]);
    expect(list[0].sessionId).toBe(sessionId);
  });

  it("hides the alerts of a machine the caller no longer manages, and does not count them", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);
    const manager = as(w.t, "manager");
    expect(await manager.query(api.notifications.unreadCount, {})).toBe(1);

    await w.t.run(async (ctx) => {
      const link = await ctx.db
        .query("machine_gestionnaires")
        .withIndex("by_machine_and_gestionnaire", (q) =>
          q.eq("machineId", w.machine).eq("gestionnaireId", w.manager),
        )
        .unique();
      if (link) await ctx.db.delete(link._id);
    });

    expect(await manager.query(api.notifications.list, {})).toEqual([]);
    expect(await manager.query(api.notifications.unreadCount, {})).toBe(0);
  });

  it("names no session, and gives no reason, that the caller cannot read", async () => {
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
      failed: true,
      reason: "emergency stop",
    });
    await delivered(w);
    // The session is moved out of the caller's organisation.
    await w.t.run((ctx) => ctx.db.patch(sessionId, { organizationId: w.orgB }));

    const [row] = await as(w.t, "manager").query(api.notifications.list, {});

    expect(row.sessionId).toBeUndefined();
    expect(row.reason).toBeUndefined();
  });

  it("counts at most UNREAD_COUNT_LIMIT unread alerts", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let i = 0; i <= UNREAD_COUNT_LIMIT; i++) {
        await ctx.db.insert("notifications", {
          organizationId: w.orgA,
          userId: w.manager,
          type: "machine_offline",
          machineId: w.machine,
          count: 1,
          createdAt: NOW,
          updatedAt: NOW + i,
          read: false,
        });
      }
    });

    expect(
      await as(w.t, "manager").query(api.notifications.unreadCount, {}),
    ).toBe(UNREAD_COUNT_LIMIT);
  });

  it("counts nothing for a caller the server places in no organisation, nor for an unknown account", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);

    expect(
      await w.t
        .withIdentity({ subject: "manager", org_id: "org_synthetic_unknown", org_role: "org:gestionnaire" })
        .query(api.notifications.unreadCount, {}),
    ).toBeNull();
    expect(
      await w.t
        .withIdentity({ subject: "nobody-yet" })
        .query(api.notifications.unreadCount, {}),
    ).toBeNull();
  });

  it("marks one of the caller's alerts as read once, and refuses a deleted one", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);
    const [own] = (await alerts(w)).filter((row) => row.userId === w.manager);
    const manager = as(w.t, "manager");

    await manager.mutation(api.notifications.markRead, { notificationId: own._id });
    await manager.mutation(api.notifications.markRead, { notificationId: own._id });
    expect((await w.t.run((ctx) => ctx.db.get(own._id)))?.read).toBe(true);

    await w.t.run((ctx) => ctx.db.delete(own._id));
    await expect(
      manager.mutation(api.notifications.markRead, { notificationId: own._id }),
    ).rejects.toThrow(/notification_not_found/);
  });
});

describe("the choice of alerts sent by e-mail", () => {
  it("defaults to every kind but the machine offline, and keeps each choice", async () => {
    const w = await seedWorld(modules);
    const manager = as(w.t, "manager");

    expect(await manager.query(api.notifications.getSettings, {})).toEqual({
      emailAvailable: false,
      email: {
        machine_offline: false,
        session_failed: true,
        drive_fault: true,
        safety_verdict: true,
      },
    });

    await manager.mutation(api.notifications.updateSettings, {
      type: "machine_offline",
      email: true,
    });
    await manager.mutation(api.notifications.updateSettings, {
      type: "drive_fault",
      email: false,
    });

    expect(
      (await manager.query(api.notifications.getSettings, {}))?.email,
    ).toEqual({
      machine_offline: true,
      session_failed: true,
      drive_fault: false,
      safety_verdict: true,
    });
    expect(emailChoices(null).machine_offline).toBe(false);
  });

  it("says e-mails are available only when the three variables are set", async () => {
    const w = await seedWorld(modules);
    setEmailVariables();

    expect(
      (await as(w.t, "manager").query(api.notifications.getSettings, {}))
        ?.emailAvailable,
    ).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// The e-mail
// ---------------------------------------------------------------------------

describe("the e-mail of an alert", () => {
  it("is off unless the key, the sender and an https address of the site are all set", () => {
    expect(emailSettings()).toBeNull();
    vi.stubEnv("RESEND_API_KEY", "re_synthetic_key");
    expect(emailSettings()).toBeNull();
    vi.stubEnv("NOTIFICATION_EMAIL_FROM", "alertes@example.invalid");
    expect(emailSettings()).toBeNull();
    vi.stubEnv("SITE_URL", "http://site.example.invalid");
    expect(emailSettings()).toBeNull();
    vi.stubEnv("SITE_URL", "https://site.example.invalid/?next=x");
    expect(emailSettings()).toBeNull();
    vi.stubEnv("SITE_URL", " https://site.example.invalid/app/ ");
    expect(emailSettings()).toEqual({
      apiKey: "re_synthetic_key",
      from: "alertes@example.invalid",
      siteUrl: "https://site.example.invalid/app",
    });
  });

  it("is queued once for a new alert, to each recipient, and carries no health data", async () => {
    const w = await seedWorld(modules);
    setEmailVariables();
    const sessionId = await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    await storeEvents(w, sessionId, [
      event(1, "verdict", "hr_critical (QUICK_STOP): 191 bpm for patient Synthetic"),
    ]);
    await storeEvents(w, sessionId, [
      event(2, "verdict", "hr_critical (GO_SILENT): 195 bpm"),
    ]);

    const rows = await alerts(w);
    expect(rows.map((row) => row.count)).toEqual([2, 2]);
    for (const row of rows) {
      const email = await emailOf(w, row.emailId);
      const user = await w.t.run((ctx) => ctx.db.get(row.userId));
      expect(email?.to).toEqual([user?.email]);
      expect(email?.from).toBe("Gaura <alertes@example.invalid>");
      expect(email?.subject).toBe(
        "[Gaura] Stop requested by the safety layer: Synthetic machine",
      );
      expect(email?.text).toContain(
        "https://site.example.invalid/en/dashboard/notifications",
      );
      const everything = JSON.stringify(email);
      for (const forbidden of [
        "bpm",
        "191",
        "195",
        "patient",
        sessionId,
        String(w.patient),
        "hr_critical",
      ]) {
        expect(everything).not.toContain(forbidden);
      }
    }
  });

  it("follows each recipient's choice, the default included, and needs an address", async () => {
    const w = await seedWorld(modules);
    setEmailVariables();
    await as(w.t, "orgAdmin").mutation(api.notifications.updateSettings, {
      type: "machine_offline",
      email: true,
    });
    await w.t.run((ctx) => ctx.db.patch(w.manager, { email: " " }));

    await markOffline(w, w.machine);
    await markOffline(w, w.orgBMachine);

    const emailed = (await alerts(w))
      .filter((row) => row.emailId !== undefined)
      .map((row) => row.userId);
    // The manager has no address; centre B kept the default: no e-mail.
    expect(emailed).toEqual([w.orgAdmin]);
  });

  it("is written in the recipient's language, on one line for the machine's name", () => {
    const french = alertEmail(
      "drive_fault",
      "fr",
      "Banc\n2\u0007",
      "https://site.example.invalid",
    );
    expect(french.subject).toBe("[Gaura] Défaut variateur : Banc 2");
    expect(french.text).toContain(
      "https://site.example.invalid/fr/dashboard/notifications",
    );
    expect(french.text).toContain("machine « Banc 2 »");
    expect(mailableName("x".repeat(200))).toHaveLength(80);
    expect(mailableName("x".repeat(200)).endsWith("…")).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// Keeping and deleting alerts
// ---------------------------------------------------------------------------

describe("how long an alert is kept", () => {
  it("is deleted 90 days after its last change, read or not", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);
    vi.setSystemTime(NOW + NOTIFICATION_RETENTION_MS - 1);
    await markOffline(w, w.orgBMachine);
    vi.setSystemTime(NOW + NOTIFICATION_RETENTION_MS + 1);

    expect(await w.t.mutation(internal.notifications.purge, {})).toEqual({
      deleted: 2,
    });
    // Left: those written a moment before the 90 days (the second check also
    // found the other machine of centre A silent).
    const left = await alerts(w);
    expect(left.length).toBeGreaterThan(0);
    expect(left.some((row) => row.machineId === w.machine)).toBe(false);
    expect(left.some((row) => row.machineId === w.orgBMachine)).toBe(true);
  });

  it("deletes 500 at a time and runs again while more are left", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      for (let i = 0; i < 501; i++) {
        await ctx.db.insert("notifications", {
          organizationId: w.orgA,
          userId: w.manager,
          type: "machine_offline",
          machineId: w.machine,
          count: 1,
          createdAt: NOW,
          updatedAt: NOW,
          read: true,
        });
      }
    });
    vi.setSystemTime(NOW + NOTIFICATION_RETENTION_MS + 1);

    expect(await w.t.mutation(internal.notifications.purge, {})).toEqual({
      deleted: 500,
    });
    expect(await alerts(w)).toHaveLength(1);
  });

  it("goes with the account that received it", async () => {
    const w = await seedWorld(modules);
    await markOffline(w, w.machine);
    await as(w.t, "manager").mutation(api.notifications.updateSettings, {
      type: "drive_fault",
      email: false,
    });

    await as(w.t, "orgAdmin").mutation(api.users.deleteUser, {
      userId: w.manager,
    });

    expect(recipients(await alerts(w))).toEqual([w.orgAdmin]);
    expect(
      await w.t.run((ctx) => ctx.db.query("notification_settings").collect()),
    ).toEqual([]);
  });
});
