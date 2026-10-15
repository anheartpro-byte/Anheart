/// <reference types="vite/client" />
/**
 * ANH-144, the Convex layer: a launch from the dashboard is checked again
 * when its machine arms it, it waits five minutes at most, and a rider and a
 * machine have one session at a time.
 *
 * - EX-1: whoever made the launch can still make it, the rider still may
 *   ride (age, max heart rate), the machine is still the one it was, and the
 *   rider is in no other session. Read again at the poll (a launch that
 *   would be refused is not offered), at the start (refused: failed with a
 *   stable code, the start answered 409 with it) and by the sweep;
 * - EX-2: a launch waits `PENDING_SESSION_TTL_S` at most (300 s by
 *   default): the sweep fails it, and the poll never offers it, even before
 *   the sweep has run;
 * - EX-3: `rider_busy` and `machine_busy`; an interrupted session holds its
 *   rider and its machine while its machine has said nothing since;
 * - centre B: the same rules across organisations, and a refusal that names
 *   nothing of the other organisation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ConvexError } from "convex/values";
import sharedText from "../contracts/machine-api.json?raw";
import { api, internal } from "./_generated/api";
import type { Doc, Id } from "./_generated/dataModel";
import crons from "./crons";
import { rolesHeldNow } from "./lib/auth";
import { CONTRACT_HEADER, CONTRACT_VERSION } from "./lib/contract";
import { DASHBOARD_ERRORS } from "./lib/errors";
import {
  ARMING_REFUSAL_CODES,
  ARMING_REFUSALS,
  DEFAULT_PENDING_SESSION_TTL_S,
  EXPIRED_REASON,
  pendingSessionTtlMs,
  POLL_MARGIN_S,
  type ArmingRefusalCode,
} from "./training";
import {
  addSession,
  as,
  configureAnheartOrganization,
  makeRiderReady,
  modules,
  NOW,
  seedMachineWorld,
  seedLaunchWorld as seedWorld,
  type World,
} from "./test.setup";

const SECOND = 1000;
const MINUTE = 60 * SECOND;
const TTL = DEFAULT_PENDING_SESSION_TTL_S * SECOND;
const MARGIN = POLL_MARGIN_S * SECOND;
const YEAR = new Date(NOW).getUTCFullYear();

const shared = JSON.parse(sharedText) as {
  arming_refusals: { route: string; status: number; codes: string[] };
  error_codes: Record<string, { statuses: number[] }>;
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
});
afterEach(() => {
  vi.useRealTimers();
  configureAnheartOrganization(null);
  delete process.env.PENDING_SESSION_TTL_S;
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type Launcher = "patient" | "manager" | "orgAdmin" | "admin";

/** `launcher` launches the seeded programme on `machineId` for `riderId` (the patient). */
function launch(
  w: World,
  launcher: Launcher = "manager",
  machineId: Id<"machines"> = w.machine,
  riderId: Id<"users"> = w.patient,
) {
  return as(w.t, launcher).mutation(api.training.launchAutoSession, {
    machineId,
    profileId: w.profileId,
    ...(launcher === "patient" ? {} : { userId: riderId }),
  });
}

const offered = (w: Pick<World, "t">, machineId: Id<"machines">) =>
  w.t.query(internal.training.getPendingTrainingSession, { machineId });

const arm = (
  w: Pick<World, "t">,
  machineId: Id<"machines">,
  sessionId: Id<"sessions">,
) =>
  w.t.mutation(internal.training.markTrainingStarted, { machineId, sessionId });

const sweep = (w: Pick<World, "t">) =>
  w.t.mutation(internal.training.sweepPendingSessions, {});

const rowOf = <Table extends "sessions" | "machines">(
  w: Pick<World, "t">,
  id: Id<Table>,
) => w.t.run((ctx) => ctx.db.get(id));

/** The data of a refused call: the stable code and its words, or the words alone. */
async function refusalOf(call: Promise<unknown>): Promise<unknown> {
  return await call.then(
    () => null,
    (error: unknown) => (error instanceof ConvexError ? error.data : error),
  );
}

const refused = (code: ArmingRefusalCode) => ({
  armed: false,
  code,
  message: ARMING_REFUSALS[code],
});

/**
 * The launch is refused when its machine arms it, with `code`: not offered,
 * failed with that code and its words, the machine left as it was, and a
 * confirmation sent again answered the same.
 */
async function expectRefusedAtArming(
  w: Pick<World, "t">,
  machineId: Id<"machines">,
  sessionId: Id<"sessions">,
  code: ArmingRefusalCode,
) {
  const machineBefore = await rowOf(w, machineId);
  expect(await offered(w, machineId)).toBeNull();

  expect(await arm(w, machineId, sessionId)).toEqual(refused(code));

  expect(await rowOf(w, sessionId)).toMatchObject({
    status: "failed",
    endedAt: Date.now(),
    endCode: code,
    endReason: ARMING_REFUSALS[code],
  });
  expect(await rowOf(w, machineId)).toEqual(machineBefore);
  expect(await arm(w, machineId, sessionId)).toEqual(refused(code));
}

/** The launch is armed: active, and its machine in session. */
async function expectArmed(
  w: Pick<World, "t">,
  machineId: Id<"machines">,
  sessionId: Id<"sessions">,
) {
  expect((await offered(w, machineId))?.sessionId).toBe(sessionId);
  expect(await arm(w, machineId, sessionId)).toEqual({ armed: true });
  expect((await rowOf(w, sessionId))?.status).toBe("active");
  expect((await rowOf(w, machineId))?.status).toBe("in_session");
}

/** Delete the rows of `table` that `match` selects. */
function deleteWhere<
  Table extends "machine_gestionnaires" | "user_gestionnaires",
>(w: World, table: Table, match: (row: Doc<Table>) => boolean) {
  return w.t.run(async (ctx) => {
    for (const row of await ctx.db.query(table).collect()) {
      if (match(row)) await ctx.db.delete(row._id);
    }
  });
}

/** Set the `active` flag of a user's membership of an organisation. */
function setMembership(
  w: World,
  userId: Id<"users">,
  organizationId: Id<"organizations">,
  active: boolean,
) {
  return w.t.run(async (ctx) => {
    const row = await ctx.db
      .query("memberships")
      .withIndex("by_user_and_organization", (q) =>
        q.eq("userId", userId).eq("organizationId", organizationId),
      )
      .unique();
    if (row === null) throw new Error("No such membership");
    await ctx.db.patch(row._id, { active });
  });
}

// ---------------------------------------------------------------------------
// The contract
// ---------------------------------------------------------------------------

describe("ANH-144 the arming refusals are codes of the machine contract", () => {
  it("lists in contracts/machine-api.json every refusal the server makes, each answered 409 by the start route", () => {
    expect(shared.arming_refusals.route).toBe(
      "POST /api/machine/training/start",
    );
    expect(shared.arming_refusals.status).toBe(409);
    expect(shared.arming_refusals.codes).toEqual(ARMING_REFUSAL_CODES);
    for (const code of ARMING_REFUSAL_CODES) {
      expect(shared.error_codes[code]?.statuses, code).toEqual([409]);
    }
  });

  it("are codes of the dashboard's table too, which the site words", () => {
    for (const code of ARMING_REFUSAL_CODES) {
      expect(DASHBOARD_ERRORS[code], code).toBeTruthy();
    }
  });

  it("writes the expiry in the words the ticket gives", () => {
    expect(ARMING_REFUSALS.expired_before_start).toBe(
      "expired before the machine armed it",
    );
    expect(EXPIRED_REASON).toBe(ARMING_REFUSALS.expired_before_start);
  });
});

// ---------------------------------------------------------------------------
// EX-1: checked again when the machine arms it
// ---------------------------------------------------------------------------

describe("ANH-144 EX-1 a launch is checked again when its machine arms it", () => {
  it("withdraws the machine's launch when the rider's launch right is withdrawn while it waits", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "patient");
    expect((await offered(w, w.machine))?.sessionId).toBe(sessionId);

    await as(w.t, "manager").mutation(api.training.revokeLaunchRight, {
      machineId: w.machine,
      userId: w.patient,
    });

    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "right_revoked_before_start",
    );
    // What the site reads: failed, with the code it words.
    expect(
      await as(w.t, "manager").query(api.training.getTrainingSession, {
        sessionId,
      }),
    ).toMatchObject({
      status: "failed",
      endCode: "right_revoked_before_start",
      canStop: false,
    });
  });

  it.each<[string, Launcher]>([
    ["the rider, holding the launch right", "patient"],
    ["a gestionnaire of the machine and of the rider", "manager"],
    ["an admin of the machine's organisation", "orgAdmin"],
    ["the Anheart admin, from another organisation", "admin"],
  ])(
    "arms a launch made by %s whose rights still hold",
    async (_who, launcher) => {
      const w = await seedWorld(modules);
      const sessionId = await launch(w, launcher);

      await expectArmed(w, w.machine, sessionId);
      expect((await rowOf(w, sessionId))?.endCode).toBeUndefined();
    },
  );

  it("arms a gestionnaire's launch for a rider who holds no launch right: the right checked is the launcher's", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "manager").mutation(api.training.revokeLaunchRight, {
      machineId: w.machine,
      userId: w.patient,
    });
    const sessionId = await launch(w, "manager");

    await expectArmed(w, w.machine, sessionId);
  });

  const STAFF_LOSSES: Array<[string, (w: World) => Promise<unknown>]> = [
    [
      "is no longer a gestionnaire of the machine",
      (w) =>
        deleteWhere(
          w,
          "machine_gestionnaires",
          (row) =>
            row.gestionnaireId === w.manager && row.machineId === w.machine,
        ),
    ],
    [
      "no longer manages the rider",
      (w) =>
        deleteWhere(
          w,
          "user_gestionnaires",
          (row) => row.gestionnaireId === w.manager && row.userId === w.patient,
        ),
    ],
    [
      "is no longer an active member of the organisation",
      (w) => setMembership(w, w.manager, w.orgA, false),
    ],
    [
      "had their account deleted in Clerk",
      (w) => w.t.run((ctx) => ctx.db.patch(w.manager, { clerkDeletedAt: NOW })),
    ],
  ];

  it.each(STAFF_LOSSES)(
    "refuses a gestionnaire's launch when the gestionnaire %s",
    async (_what, lose) => {
      const w = await seedWorld(modules);
      const sessionId = await launch(w, "manager");

      await lose(w);

      await expectRefusedAtArming(
        w,
        w.machine,
        sessionId,
        "right_revoked_before_start",
      );
    },
  );

  it("refuses the Anheart admin's launch once they are no longer an admin of Anheart", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "admin");

    await setMembership(w, w.admin, w.anheartOrg, false);

    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "right_revoked_before_start",
    );
  });

  const RIDER_LOSSES: Array<[string, (w: World) => Promise<unknown>]> = [
    [
      "is no longer an active member of the machine's organisation",
      (w) => setMembership(w, w.patient, w.orgA, false),
    ],
    [
      "had their account deleted in Clerk",
      (w) => w.t.run((ctx) => ctx.db.patch(w.patient, { clerkDeletedAt: NOW })),
    ],
    [
      "no longer has an account",
      (w) => w.t.run((ctx) => ctx.db.delete(w.patient)),
    ],
  ];

  it.each(RIDER_LOSSES)(
    "refuses a launch whose rider %s",
    async (_what, lose) => {
      const w = await seedWorld(modules);
      const sessionId = await launch(w, "manager");

      await lose(w);

      await expectRefusedAtArming(
        w,
        w.machine,
        sessionId,
        "right_revoked_before_start",
      );
    },
  );

  it("refuses a waiting launch that names no launcher: nobody's right can be checked", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");
    await w.t.run((ctx) => ctx.db.patch(sessionId, { startedById: undefined }));

    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "right_revoked_before_start",
    );
  });

  it("refuses a launch whose machine moved to another organisation", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");

    await w.t.run((ctx) => ctx.db.patch(w.machine, { organizationId: w.orgB }));

    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "organization_changed_before_start",
    );
  });

  it("refuses a launch whose machine was deleted, which the sweep fails with that code", async () => {
    const w = await seedWorld(modules);
    const armed = await launch(w, "manager");
    const swept = await launch(w, "orgAdmin", w.otherMachine, w.otherPatient);

    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.machine, { isDeleted: true });
      await ctx.db.patch(w.otherMachine, { isDeleted: true });
    });

    await expectRefusedAtArming(
      w,
      w.machine,
      armed,
      "machine_deleted_before_start",
    );
    expect(await sweep(w)).toEqual({ failed: 1 });
    expect(await rowOf(w, swept)).toMatchObject({
      status: "failed",
      endCode: "machine_deleted_before_start",
    });
  });

  it.each<[string, { birthYear?: number }]>([
    ["is no longer known", { birthYear: undefined }],
    ["now gives a minor", { birthYear: YEAR - 15 }],
  ])(
    "refuses a launch when the rider's birth year %s",
    async (_what, change) => {
      const w = await seedWorld(modules);
      const sessionId = await launch(w, "manager");

      await w.t.run((ctx) => ctx.db.patch(w.patient, change));

      await expectRefusedAtArming(
        w,
        w.machine,
        sessionId,
        "age_refused_before_start",
      );
    },
  );

  it.each<[string, { hrMax?: number; birthYear?: number }, ArmingRefusalCode]>([
    ["was lowered", { hrMax: 175 }, "hr_max_changed_before_start"],
    ["was raised", { hrMax: 185 }, "hr_max_changed_before_start"],
    // The estimate from the birth year is then used: 182 bpm, not 180.
    [
      "was cleared, leaving the estimate",
      { hrMax: undefined },
      "hr_max_changed_before_start",
    ],
    // Without a birth year either, the age is refused first.
    [
      "can no longer be known",
      { hrMax: undefined, birthYear: undefined },
      "age_refused_before_start",
    ],
  ])(
    "refuses a launch when the rider's max heart rate %s: the machine would fit the programme to the old one",
    async (_what, change, code) => {
      const w = await seedWorld(modules);
      const sessionId = await launch(w, "manager");
      expect((await rowOf(w, sessionId))?.subjectHrMax).toBe(180);

      await w.t.run((ctx) => ctx.db.patch(w.patient, change));

      await expectRefusedAtArming(w, w.machine, sessionId, code);
    },
  );

  it("refuses a launch whose rider has since started a session on another machine", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");

    await addSession(w, {
      machineId: w.otherMachine,
      userId: w.patient,
      status: "active",
      kind: "manual",
    });

    await expectRefusedAtArming(w, w.machine, sessionId, "rider_busy");
  });

  it("answers the start of a launch already settled as before: unknown, or no longer pending", async () => {
    const w = await seedWorld(modules);
    const cancelled = await launch(w, "manager");
    await as(w.t, "manager").mutation(api.training.requestStop, {
      sessionId: cancelled,
    });

    // Cancelled on the site: no code of the arming, the refusal it always had.
    expect(await refusalOf(arm(w, w.machine, cancelled))).toEqual({
      code: "session_not_pending",
      message: "Session is not pending (status: failed)",
    });
    expect(await refusalOf(arm(w, w.otherMachine, cancelled))).toEqual({
      code: "session_not_found",
      message: "Session not found",
    });
  });
});

describe("ANH-144 EX-1 the start route answers a refusal at arming with 409", () => {
  it("withdraws the launch, answers 409 with the code, and the machine is not put in session", async () => {
    const w = await seedMachineWorld(modules);
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      profileId: w.profileId,
      userId: w.patient,
    });
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.patient, { hrMax: 170 });
    });

    const response = await w.t.fetch("/api/machine/training/start", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${w.machineKey}`,
        "Content-Type": "application/json",
        [CONTRACT_HEADER]: CONTRACT_VERSION,
      },
      body: JSON.stringify({ sessionId }),
    });

    expect(response.status).toBe(409);
    expect(await response.json()).toEqual({
      error: "hr_max_changed_before_start",
      message: ARMING_REFUSALS.hr_max_changed_before_start,
    });
    expect((await rowOf(w, sessionId))?.status).toBe("failed");
    expect((await rowOf(w, w.machine))?.status).toBe("online");
  });
});

// ---------------------------------------------------------------------------
// EX-2: a launch waits five minutes at most
// ---------------------------------------------------------------------------

describe("ANH-144 EX-2 a launch waits PENDING_SESSION_TTL_S at most", () => {
  it("is offered until 45 s before the limit, its start confirmed until the limit, and refused after it, even before the sweep", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");
    expect(MARGIN).toBe(45 * SECOND);

    vi.setSystemTime(NOW + TTL - MARGIN);
    expect((await offered(w, w.machine))?.sessionId).toBe(sessionId);

    // Within the margin: no longer offered, so that a console that arms what
    // it reads does not start a launch its confirmation would find expired.
    vi.setSystemTime(NOW + TTL - MARGIN + 1);
    expect(await offered(w, w.machine)).toBeNull();
    expect(await sweep(w)).toEqual({ failed: 0 });
    expect((await rowOf(w, sessionId))?.status).toBe("pending");

    vi.setSystemTime(NOW + TTL + 1);
    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "expired_before_start",
    );
    expect((await rowOf(w, sessionId))?.endReason).toBe(
      "expired before the machine armed it",
    );
  });

  it("is failed by the sweep, with the refusals of EX-1, and nothing else is touched", async () => {
    const w = await seedWorld(modules);
    const expiring = await launch(w, "manager");
    vi.setSystemTime(NOW + 2 * MINUTE);
    const fresh = await launch(w, "orgAdmin", w.otherMachine, w.otherPatient);
    // Launched by its rider: withdrawing the rider's right withdraws the launch.
    const revoked = await as(w.t, "orgBPatient").mutation(
      api.training.launchAutoSession,
      { machineId: w.orgBMachine, profileId: w.profileId },
    );
    await as(w.t, "orgBManager").mutation(api.training.revokeLaunchRight, {
      machineId: w.orgBMachine,
      userId: w.orgBPatient,
    });
    // A pending session of the retired recording mode is history: untouched.
    const legacy = await addSession(w, {
      machineId: w.machine,
      userId: w.stranger,
      status: "pending",
    });
    const running = await addSession(w, {
      machineId: w.otherMachine,
      userId: w.stranger,
      status: "active",
      kind: "manual",
    });
    const before = {
      fresh: await rowOf(w, fresh),
      legacy: await rowOf(w, legacy),
      running: await rowOf(w, running),
    };

    vi.setSystemTime(NOW + TTL + SECOND);
    expect(await sweep(w)).toEqual({ failed: 2 });

    expect(await rowOf(w, expiring)).toMatchObject({
      status: "failed",
      endedAt: NOW + TTL + SECOND,
      endCode: "expired_before_start",
      endReason: "expired before the machine armed it",
    });
    expect(await rowOf(w, revoked)).toMatchObject({
      status: "failed",
      endCode: "right_revoked_before_start",
    });
    expect({
      fresh: await rowOf(w, fresh),
      legacy: await rowOf(w, legacy),
      running: await rowOf(w, running),
    }).toEqual(before);
    // Run again: nothing more to fail.
    expect(await sweep(w)).toEqual({ failed: 0 });
  });

  it("acceptance: a launch whose machine is offline for ten minutes is marked expired, and the machine back online does not take it", async () => {
    const w = await seedMachineWorld(modules);
    const machineCall = (
      method: "GET" | "POST",
      path: string,
      body?: unknown,
    ) =>
      w.t.fetch(path, {
        method,
        headers: {
          Authorization: `Bearer ${w.machineKey}`,
          "Content-Type": "application/json",
          [CONTRACT_HEADER]: CONTRACT_VERSION,
        },
        body: body === undefined ? undefined : JSON.stringify(body),
      });
    const sessionId = await w.admin.mutation(api.training.launchAutoSession, {
      machineId: w.machine,
      profileId: w.profileId,
      userId: w.patient,
    });
    // The machine reads the launch, then goes silent for ten minutes.
    const read = (await (
      await machineCall("GET", "/api/machine/training/poll")
    ).json()) as { session: { sessionId: string } | null };
    expect(read.session?.sessionId).toBe(sessionId);

    for (let minute = 1; minute <= 10; minute++) {
      vi.setSystemTime(NOW + minute * MINUTE);
      await w.t.mutation(internal.machines.checkOfflineMachines, {});
      await sweep(w);
    }
    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "failed",
      endedAt: NOW + 6 * MINUTE,
      endCode: "expired_before_start",
      endReason: "expired before the machine armed it",
    });
    expect((await rowOf(w, w.machine))?.status).toBe("offline");

    // Back online: nothing is offered, and the launch it had read is refused.
    expect(
      (await machineCall("POST", "/api/machine/heartbeat", {})).status,
    ).toBe(200);
    const polled = await machineCall("GET", "/api/machine/training/poll");
    expect(((await polled.json()) as { session: unknown }).session).toBeNull();
    const started = await machineCall("POST", "/api/machine/training/start", {
      sessionId,
    });
    expect(started.status).toBe(409);
    expect(await started.json()).toEqual({
      error: "expired_before_start",
      message: "expired before the machine armed it",
    });
    expect((await rowOf(w, sessionId))?.status).toBe("failed");
  });

  it("confirms a launch read just before the margin, until the limit itself", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");
    vi.setSystemTime(NOW + TTL - MARGIN);
    expect((await offered(w, w.machine))?.sessionId).toBe(sessionId);

    vi.setSystemTime(NOW + TTL);
    expect(await arm(w, w.machine, sessionId)).toEqual({ armed: true });
    expect(await rowOf(w, sessionId)).toMatchObject({
      status: "active",
      startedAt: NOW + TTL,
    });
  });

  it("follows PENDING_SESSION_TTL_S when the deployment sets it", async () => {
    process.env.PENDING_SESSION_TTL_S = "120";
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");

    vi.setSystemTime(NOW + 2 * MINUTE - MARGIN);
    expect((await offered(w, w.machine))?.sessionId).toBe(sessionId);
    vi.setSystemTime(NOW + 2 * MINUTE - MARGIN + 1);
    expect(await offered(w, w.machine)).toBeNull();
    vi.setSystemTime(NOW + 2 * MINUTE);
    expect(await sweep(w)).toEqual({ failed: 0 });
    vi.setSystemTime(NOW + 2 * MINUTE + 1);
    expect(await sweep(w)).toEqual({ failed: 1 });
  });

  it.each<[string | undefined, number]>([
    [undefined, 300],
    ["", 300],
    ["90", 90],
    [" 120 ", 120],
    ["600", 600],
    ["89", 300],
    ["60", 300],
    ["601", 300],
    ["0", 300],
    ["-60", 300],
    ["1.5", 300],
    ["1e2", 300],
    ["five minutes", 300],
  ])("reads PENDING_SESSION_TTL_S=%j as %i s", (value, seconds) => {
    if (value === undefined) delete process.env.PENDING_SESSION_TTL_S;
    else process.env.PENDING_SESSION_TTL_S = value;
    expect(pendingSessionTtlMs()).toBe(seconds * SECOND);
  });

  it("runs the sweep every minute", () => {
    const jobs = (
      crons as unknown as {
        crons: Record<string, { name: string; schedule: unknown }>;
      }
    ).crons;
    expect(jobs["sweep-pending-sessions"]).toEqual({
      name: "training:sweepPendingSessions",
      args: [{}],
      schedule: { type: "interval", minutes: 1 },
    });
  });
});

// ---------------------------------------------------------------------------
// EX-3: one session at a time, for a rider and for a machine
// ---------------------------------------------------------------------------

// The data of the refusal: the code alone, as `refusal` throws it
// (convex/lib/errors.ts), which the site translates.
const RIDER_BUSY = "rider_busy";
const MACHINE_BUSY = "machine_busy";

describe("ANH-144 EX-3 a rider has one session at a time, on any machine", () => {
  it("refuses a second launch for a rider who has one waiting on another machine, and queues nothing", async () => {
    const w = await seedWorld(modules);
    const first = await launch(w, "manager");

    expect(
      await refusalOf(launch(w, "orgAdmin", w.otherMachine, w.patient)),
    ).toEqual(RIDER_BUSY);
    const sessions = await w.t.run((ctx) => ctx.db.query("sessions").collect());
    expect(sessions.map((s) => s._id)).toEqual([first]);
  });

  it.each<[string, "active" | "interrupted"]>([
    ["running", "active"],
    ["interrupted, on a machine silent since", "interrupted"],
  ])(
    "refuses a launch for a rider whose session on another machine is %s",
    async (_what, status) => {
      const w = await seedWorld(modules);
      const other = await addSession(w, {
        machineId: w.otherMachine,
        userId: w.patient,
        status,
        kind: "auto",
      });
      if (status === "interrupted") {
        // Its machine's last heartbeat is no later than the interruption.
        await w.t.run((ctx) => ctx.db.patch(other, { interruptedAt: NOW }));
      }

      expect(await refusalOf(launch(w, "manager"))).toEqual(RIDER_BUSY);
    },
  );

  it.each<[string, (w: World, other: Id<"sessions">) => Promise<unknown>]>([
    [
      "interrupted on a machine heard since, which therefore does not run it",
      (w, other) =>
        w.t.run(async (ctx) => {
          await ctx.db.patch(other, {
            status: "interrupted",
            interruptedAt: NOW - MINUTE,
          });
          await ctx.db.patch(w.otherMachine, { lastHeartbeat: NOW });
        }),
    ],
    [
      "a pending session of the retired recording mode, which holds its machine only",
      (w, other) =>
        w.t.run((ctx) =>
          ctx.db.patch(other, { status: "pending", kind: undefined }),
        ),
    ],
    [
      "completed",
      (w, other) =>
        w.t.run((ctx) => ctx.db.patch(other, { status: "completed" })),
    ],
    [
      "failed",
      (w, other) => w.t.run((ctx) => ctx.db.patch(other, { status: "failed" })),
    ],
    [
      "a launch that has waited too long, failed on the way",
      (w, other) =>
        w.t.run((ctx) =>
          ctx.db.patch(other, {
            status: "pending",
            origin: "remote",
            startedAt: NOW - TTL - 1,
          }),
        ),
    ],
  ])(
    "accepts a launch for a rider whose other session is %s",
    async (_what, settle) => {
      const w = await seedWorld(modules);
      const other = await addSession(w, {
        machineId: w.otherMachine,
        userId: w.patient,
        status: "active",
        kind: "auto",
      });
      await settle(w, other);
      const before = await rowOf(w, other);

      const sessionId = await launch(w, "manager");

      expect((await rowOf(w, sessionId))?.status).toBe("pending");
      const after = await rowOf(w, other);
      if (before?.status === "pending" && before.kind === "auto") {
        expect(after).toMatchObject({
          status: "failed",
          endedAt: NOW,
          endCode: "expired_before_start",
        });
      } else {
        expect(after).toEqual(before);
      }
    },
  );
});

describe("ANH-144 EX-3 a machine has one session at a time", () => {
  it.each<[string, "pending" | "active" | "interrupted"]>([
    ["a launch waiting", "pending"],
    ["a session running, whatever its last heartbeat said", "active"],
    ["a session interrupted, the machine silent since", "interrupted"],
  ])("refuses a launch on a machine with %s", async (_what, status) => {
    const w = await seedWorld(modules);
    const other = await addSession(w, {
      machineId: w.machine,
      userId: w.stranger,
      status,
      kind: "auto",
    });
    if (status === "interrupted") {
      await w.t.run((ctx) => ctx.db.patch(other, { interruptedAt: NOW }));
    }

    expect(await refusalOf(launch(w, "manager"))).toEqual(MACHINE_BUSY);
  });

  it("accepts a launch on a machine whose interrupted session it no longer runs: it was heard since", async () => {
    const w = await seedWorld(modules);
    const other = await addSession(w, {
      machineId: w.machine,
      userId: w.stranger,
      status: "interrupted",
      kind: "auto",
    });
    await w.t.run(async (ctx) => {
      await ctx.db.patch(other, { interruptedAt: NOW - MINUTE });
      await ctx.db.patch(w.machine, { lastHeartbeat: NOW - SECOND });
    });

    expect((await rowOf(w, await launch(w, "manager")))?.status).toBe(
      "pending",
    );
  });

  it("does not count a launch failed before its start as running or waiting", async () => {
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");
    await w.t.run((ctx) => ctx.db.patch(w.patient, { hrMax: 175 }));
    await arm(w, w.machine, sessionId);

    expect(
      (await as(w.t, "manager").query(api.dashboard.counters, {})).sessions,
    ).toEqual({ active: 0, pending: 0 });
  });
});

// ---------------------------------------------------------------------------
// Centre B
// ---------------------------------------------------------------------------

describe("ANH-144 the same rules in centre B, and across organisations", () => {
  it("lets centre B launch while centre A has sessions of its own riders and machines", async () => {
    const w = await seedWorld(modules);
    await launch(w, "manager");

    const sessionId = await as(w.t, "orgBManager").mutation(
      api.training.launchAutoSession,
      {
        machineId: w.orgBMachine,
        profileId: w.profileId,
        userId: w.orgBPatient,
      },
    );

    await expectArmed(w, w.orgBMachine, sessionId);
  });

  it("refuses in centre B a rider shared with centre A who rides there, and names nothing of centre A", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      });
      await ctx.db.insert("user_gestionnaires", {
        organizationId: w.orgB,
        userId: w.patient,
        gestionnaireId: w.orgBManager,
        createdAt: NOW,
        createdBy: w.orgBAdmin,
      });
    });
    await makeRiderReady(w.t, w.patient, w.orgB, {
      now: NOW,
      validUntil: Date.UTC(2100, 0, 1),
    });
    await addSession(w, {
      machineId: w.machine,
      userId: w.patient,
      status: "active",
      kind: "auto",
    });

    const answer = await refusalOf(
      as(w.t, "orgBAdmin").mutation(api.training.launchAutoSession, {
        machineId: w.orgBMachine,
        profileId: w.profileId,
        userId: w.patient,
      }),
    );

    expect(answer).toEqual(RIDER_BUSY);
    expect(JSON.stringify(answer)).not.toMatch(/Centre A|Synthetic machine/);
  });

  it("withdraws a launch of centre B whose right centre B withdrew", async () => {
    const w = await seedWorld(modules);
    const sessionId = await as(w.t, "orgBPatient").mutation(
      api.training.launchAutoSession,
      { machineId: w.orgBMachine, profileId: w.profileId },
    );

    await as(w.t, "orgBManager").mutation(api.training.revokeLaunchRight, {
      machineId: w.orgBMachine,
      userId: w.orgBPatient,
    });

    await expectRefusedAtArming(
      w,
      w.orgBMachine,
      sessionId,
      "right_revoked_before_start",
    );
  });

  it("does not let a right held in centre B carry a launch on a machine of centre A", async () => {
    // The gestionnaire of centre B is made a member of centre A as a patient:
    // what they may do in centre B counts for nothing on centre A's machine.
    const w = await seedWorld(modules);
    const sessionId = await launch(w, "manager");
    await w.t.run(async (ctx) => {
      await ctx.db.patch(sessionId, { startedById: w.orgBManager });
      await ctx.db.insert("memberships", {
        userId: w.orgBManager,
        organizationId: w.orgA,
        role: "user",
        active: true,
      });
    });

    await expectRefusedAtArming(
      w,
      w.machine,
      sessionId,
      "right_revoked_before_start",
    );
  });
});

// ---------------------------------------------------------------------------
// The roles an account holds now, read from the server's rows
// ---------------------------------------------------------------------------

describe("ANH-144 rolesHeldNow", () => {
  const rolesOf = (
    w: World,
    userId: Id<"users">,
    organizationId: Id<"organizations">,
  ) =>
    w.t.run(async (ctx) =>
      (await rolesHeldNow(ctx, userId, organizationId)).map((user) => ({
        role: user.role,
        organizationId: user.organizationId,
      })),
    );

  it("is the role held in the organisation, and the Anheart admin's anywhere", async () => {
    const w = await seedWorld(modules);
    expect(await rolesOf(w, w.manager, w.orgA)).toEqual([
      { role: "gestionnaire", organizationId: w.orgA },
    ]);
    expect(await rolesOf(w, w.orgAdmin, w.orgA)).toEqual([
      { role: "org_admin", organizationId: w.orgA },
    ]);
    expect(await rolesOf(w, w.admin, w.orgA)).toEqual([
      { role: "admin", organizationId: w.anheartOrg },
    ]);
    expect(await rolesOf(w, w.orgAdmin, w.orgB)).toEqual([]);
  });

  it("is nothing in an organisation deleted in Clerk, and nothing for an account that is gone", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      await ctx.db.patch(w.orgA, { deletedAt: NOW });
      await ctx.db.patch(w.anheartOrg, { deletedAt: NOW });
    });
    expect(await rolesOf(w, w.manager, w.orgA)).toEqual([]);
    expect(await rolesOf(w, w.admin, w.orgA)).toEqual([]);

    await w.t.run((ctx) => ctx.db.delete(w.orgBManager));
    expect(await rolesOf(w, w.orgBManager, w.orgB)).toEqual([]);
  });
});
