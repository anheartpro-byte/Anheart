/**
 * Training sessions on the centrifuge: who may launch them, how they reach
 * the machine, and what comes back.
 *
 * Two kinds of training session, and the asymmetry between them is the point:
 *
 * - **auto**: one of the machine's pre-saved programmes (synced from the Pi's
 *   ProfileStore). The heart rate drives the speed: the Pi's controller holds
 *   the rider in the programme's zone (e.g. 145-155 bpm, "a jog"). An auto
 *   session may be launched from this dashboard, by an admin, by a
 *   gestionnaire of the machine, or by a user a gestionnaire has granted the
 *   launch right to.
 * - **manual**: the operator sets the speed directly. It can ONLY be started
 *   at the machine, on the local panel, by someone standing next to it. There
 *   is deliberately no mutation here that creates one; the Pi registers a
 *   manual session it has already started (`registerLocalSession`) so the
 *   dashboard can show it.
 *
 * The Pi remains the authority on safety: everything checked here (rights,
 * zone against the rider's max heart rate, machine state) is checked again on
 * the Pi, which can still refuse the start (e-stop wiring not attested, a
 * standing safety verdict, a drive fault...). A refusal comes back as a
 * failed session with the Pi's reason.
 */
import { ConvexError, v } from "convex/values";
import {
  internalMutation,
  internalQuery,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import {
  canAccessMachine,
  canAccessSession,
  canAccessUser,
  canManageMachine,
  getCurrentUserOrThrow,
  inScope,
  organizationRoleOf,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";
import { liveStateValidator, machineProfileFields } from "./schema";
import { authorizedMachineLive } from "./lib/trainingPrivacy";
import { machineError } from "./lib/contract";
// A heartbeat older than this means the machine cannot be relied on to answer.
// The dashboard judges freshness on the same value (hooks/use-freshness.ts).
import { LIVE_FRESH_MS } from "../lib/training";

// ---------------------------------------------------------------------------
// Physiology: mirrors raspberry-pi/src/training/plan.py. The Pi re-checks all
// of it; these copies exist so a dashboard user gets the refusal at once.
// ---------------------------------------------------------------------------

/** zone_high_bpm may not exceed this fraction of the rider's max heart rate. */
export const ZONE_CEILING_FRACTION = 0.9;
const HR_MAX_MIN = 100;
const HR_MAX_MAX = 220;
const MIN_AGE = 10;
const MAX_AGE = 100;

/**
 * A max heart rate and a birth year are counted, not measured in fractions:
 * each is a finite whole number, or it is not a value. It is the only kind
 * `setUserPhysiology` stores, and the only kind read back from an account: a
 * value on record that is anything else counts as not set, so that every
 * check below compares numbers.
 */
function isWholeNumber(value: unknown): value is number {
  return Number.isInteger(value);
}

/**
 * Measured maximum if known, else the Tanaka estimate (208 - 0.7 x age). A
 * measured value that cannot be used (out of range, not a whole number) gives
 * nothing: it is not replaced by the estimate.
 */
export function effectiveHrMax(
  user: Pick<Doc<"users">, "hrMax" | "birthYear">,
  now: number,
): number | null {
  if (user.hrMax !== undefined) {
    return isWholeNumber(user.hrMax) &&
      user.hrMax >= HR_MAX_MIN &&
      user.hrMax <= HR_MAX_MAX
      ? user.hrMax
      : null;
  }
  if (!isWholeNumber(user.birthYear)) return null;
  const age = new Date(now).getUTCFullYear() - user.birthYear;
  if (age < MIN_AGE || age > MAX_AGE) return null;
  return Math.round(208 - 0.7 * age);
}

/**
 * Why a programme is unsafe for this rider, or null if it is acceptable.
 *
 * Each rule is written as what is accepted, negated: a programme passes only
 * if its value is known to be at or under the limit. A value that is not a
 * number on either side is therefore a refusal.
 */
export function zoneRefusal(
  profile: { zoneHighBpm: number; hardMaxBpm: number },
  hrMax: number,
): string | null {
  const ceiling = Math.floor(ZONE_CEILING_FRACTION * hrMax);
  if (!(profile.zoneHighBpm <= ceiling)) {
    return `Zone up to ${profile.zoneHighBpm} bpm exceeds 90% of this rider's max heart rate (${hrMax} bpm → ceiling ${ceiling} bpm)`;
  }
  if (!(profile.hardMaxBpm <= hrMax)) {
    return `Programme hard maximum ${profile.hardMaxBpm} bpm is above this rider's max heart rate (${hrMax} bpm)`;
  }
  return null;
}

/**
 * Youngest rider a remote AUTO launch accepts. Mirrors the Pi's MIN_RIDER_AGE
 * default; the Pi, whose value is configured per machine, checks again and is
 * the authority. [MED] lowering it is a medical decision.
 */
export const MIN_RIDER_AGE = 18;

/**
 * Age in whole years from a birth year (the later birthday assumed: never
 * older). Null when the birth year is not set, or is not a whole number.
 */
export function ageFrom(
  birthYear: number | undefined,
  now: number,
): number | null {
  if (!isWholeNumber(birthYear)) return null;
  return new Date(now).getUTCFullYear() - birthYear - 1;
}

// ---------------------------------------------------------------------------
// Rights
// ---------------------------------------------------------------------------

async function hasLaunchRight(
  ctx: QueryCtx | MutationCtx,
  machineId: Id<"machines">,
  userId: Id<"users">,
): Promise<boolean> {
  const row = await ctx.db
    .query("machine_user_permissions")
    .withIndex("by_machine_and_user", (q) =>
      q.eq("machineId", machineId).eq("userId", userId),
    )
    .unique();
  return row !== null;
}

/**
 * A user's launch right on a machine counts only inside the machine's
 * organisation: the right, the machine and the call are all of the same one.
 */
async function holdsLaunchRight(
  ctx: QueryCtx | MutationCtx,
  user: CurrentUser,
  machineId: Id<"machines">,
): Promise<boolean> {
  const machine = await ctx.db.get(machineId);
  return (
    machine !== null &&
    sameOrganization(user, machine.organizationId) &&
    (await hasLaunchRight(ctx, machineId, user._id))
  );
}

/** Admin, gestionnaire of the machine, or a user holding the launch right. */
async function canSeeMachineTraining(
  ctx: QueryCtx | MutationCtx,
  user: CurrentUser,
  machineId: Id<"machines">,
): Promise<boolean> {
  if (await canAccessMachine(ctx, machineId, user)) return true;
  return user.role === "user" && (await holdsLaunchRight(ctx, user, machineId));
}

function fullName(user: Pick<Doc<"users">, "firstName" | "lastName">) {
  return `${user.firstName} ${user.lastName}`.trim();
}

export const grantLaunchRight = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId, me))) {
      throw new ConvexError(
        "Only an admin or a manager of this machine can grant launch rights",
      );
    }
    const machine = await ctx.db.get(args.machineId);
    if (!machine) throw new ConvexError("Machine not found");
    // The right goes to a user of the machine's organisation.
    const targetRole = await organizationRoleOf(
      ctx,
      args.userId,
      machine.organizationId,
    );
    if (targetRole !== "user") {
      throw new ConvexError(
        "Launch rights are granted to users; managers and admins already have them",
      );
    }
    // A gestionnaire may only grant rights to a patient they manage.
    if (!(await canAccessUser(ctx, args.userId, me))) {
      throw new ConvexError("You do not manage this user");
    }
    if (await hasLaunchRight(ctx, args.machineId, args.userId)) return null;
    await ctx.db.insert("machine_user_permissions", {
      organizationId: machine.organizationId,
      machineId: args.machineId,
      userId: args.userId,
      grantedBy: me._id,
      createdAt: Date.now(),
    });
    return null;
  },
});

export const revokeLaunchRight = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId, me))) {
      throw new ConvexError(
        "Only an admin or a manager of this machine can revoke launch rights",
      );
    }
    const row = await ctx.db
      .query("machine_user_permissions")
      .withIndex("by_machine_and_user", (q) =>
        q.eq("machineId", args.machineId).eq("userId", args.userId),
      )
      .unique();
    if (row) await ctx.db.delete(row._id);
    return null;
  },
});

export const listLaunchRights = query({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      userId: v.id("users"),
      name: v.string(),
      email: v.string(),
      hrMax: v.union(v.number(), v.null()),
      grantedByName: v.string(),
      createdAt: v.number(),
    }),
  ),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canAccessMachine(ctx, args.machineId, me))) return [];
    const rows = await ctx.db
      .query("machine_user_permissions")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    const now = Date.now();
    const result = [];
    for (const row of rows) {
      const user = await ctx.db.get(row.userId);
      if (!user) continue;
      const granter = await ctx.db.get(row.grantedBy);
      result.push({
        userId: user._id,
        name: fullName(user),
        email: user.email,
        hrMax: effectiveHrMax(user, now),
        grantedByName: granter ? fullName(granter) : "-",
        createdAt: row.createdAt,
      });
    }
    return result;
  },
});

/** Set the physiology used to vet a zone. Admin, or a gestionnaire of the user. */
export const setUserPhysiology = mutation({
  args: {
    userId: v.id("users"),
    hrMax: v.optional(v.union(v.number(), v.null())),
    birthYear: v.optional(v.union(v.number(), v.null())),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (me.role === "user")
      throw new ConvexError("Only a manager can set physiology");
    if (!(await canAccessUser(ctx, args.userId, me))) {
      throw new ConvexError("You do not manage this user");
    }
    // Each value is a whole number first, then within its range; null clears
    // it. Both are checked before anything is written.
    const patch: Partial<Pick<Doc<"users">, "hrMax" | "birthYear">> = {};
    if (args.hrMax !== undefined) {
      if (args.hrMax !== null) {
        if (!isWholeNumber(args.hrMax)) {
          throw new ConvexError("Max heart rate must be a whole number of bpm");
        }
        if (args.hrMax < HR_MAX_MIN || args.hrMax > HR_MAX_MAX) {
          throw new ConvexError(
            `Max heart rate must be within ${HR_MAX_MIN}-${HR_MAX_MAX} bpm`,
          );
        }
      }
      patch.hrMax = args.hrMax ?? undefined;
    }
    if (args.birthYear !== undefined) {
      if (args.birthYear !== null) {
        if (!isWholeNumber(args.birthYear)) {
          throw new ConvexError("Birth year must be a whole number");
        }
        const age = new Date().getUTCFullYear() - args.birthYear;
        if (age < MIN_AGE || age > MAX_AGE) {
          throw new ConvexError("Birth year gives an implausible age");
        }
      }
      patch.birthYear = args.birthYear ?? undefined;
    }
    await ctx.db.patch(args.userId, patch);
    return null;
  },
});

// ---------------------------------------------------------------------------
// What a dashboard user can launch
// ---------------------------------------------------------------------------

const profileValidator = v.object(machineProfileFields);

function profileOf(row: Doc<"machine_profiles">) {
  return {
    profileId: row.profileId,
    name: row.name,
    totalDurationS: row.totalDurationS,
    zoneLowBpm: row.zoneLowBpm,
    zoneHighBpm: row.zoneHighBpm,
    hardMaxBpm: row.hardMaxBpm,
    criticalBpm: row.criticalBpm,
    subjectHrMax: row.subjectHrMax,
    minRunRpm: row.minRunRpm,
    maxRpm: row.maxRpm,
  };
}

async function profilesFor(ctx: QueryCtx, machineId: Id<"machines">) {
  const rows = await ctx.db
    .query("machine_profiles")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .collect();
  return rows.map(profileOf).sort((a, b) => a.name.localeCompare(b.name));
}

export const listMachineProfiles = query({
  args: { machineId: v.id("machines") },
  returns: v.array(profileValidator),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canSeeMachineTraining(ctx, me, args.machineId))) return [];
    return await profilesFor(ctx, args.machineId);
  },
});

/**
 * Machines the current user may launch an auto session on, with their live
 * state and presets. Anheart admin: every machine. Inside the caller's
 * organisation only: every machine for its admin, the ones they manage for a
 * gestionnaire, the ones they hold the launch right for for a user.
 */
export const listLaunchableMachines = query({
  args: {},
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      location: v.optional(v.string()),
      status: v.string(),
      // Server-dated, with the server's clock in this answer: the dashboard
      // judges the age of the signal and of `live` on them, never on the
      // clock of the computer it runs on.
      lastHeartbeat: v.number(),
      serverNow: v.number(),
      programsEnabled: v.boolean(),
      live: v.union(liveStateValidator, v.null()),
      profiles: v.array(profileValidator),
      myHrMax: v.union(v.number(), v.null()),
    }),
  ),
  handler: async (ctx) => {
    const me = await getCurrentUserOrThrow(ctx);
    let machineIds: Id<"machines">[];
    if (me.role === "admin") {
      machineIds = (await ctx.db.query("machines").collect()).map((m) => m._id);
    } else if (me.role === "org_admin") {
      machineIds = (
        await ctx.db
          .query("machines")
          .withIndex("by_organization", (q) =>
            q.eq("organizationId", me.organizationId),
          )
          .collect()
      ).map((m) => m._id);
    } else if (me.role === "gestionnaire") {
      machineIds = (
        await ctx.db
          .query("machine_gestionnaires")
          .withIndex("by_gestionnaire", (q) => q.eq("gestionnaireId", me._id))
          .collect()
      ).map((r) => r.machineId);
    } else {
      machineIds = (
        await ctx.db
          .query("machine_user_permissions")
          .withIndex("by_user", (q) => q.eq("userId", me._id))
          .collect()
      ).map((r) => r.machineId);
    }
    const now = Date.now();
    const result = [];
    for (const id of machineIds) {
      const machine = await ctx.db.get(id);
      if (!machine || machine.isDeleted) continue;
      if (!inScope(me, machine.organizationId)) continue;
      result.push({
        _id: machine._id,
        name: machine.name,
        location: machine.location,
        status: machine.status,
        lastHeartbeat: machine.lastHeartbeat,
        serverNow: now,
        programsEnabled: machine.programsEnabled ?? false,
        live:
          machine.live && now - machine.live.updatedAt < LIVE_FRESH_MS
            ? await authorizedMachineLive(ctx, me, machine)
            : null,
        profiles: await profilesFor(ctx, id),
        myHrMax: effectiveHrMax(me, now),
      });
    }
    return result;
  },
});

// ---------------------------------------------------------------------------
// Launch and stop
// ---------------------------------------------------------------------------

/**
 * Queue an AUTO session for the machine to pick up. The only remote launch.
 *
 * A user launches for themselves only; an admin or a gestionnaire launches
 * for a rider they manage (or for themselves).
 */
export const launchAutoSession = mutation({
  args: {
    machineId: v.id("machines"),
    profileId: v.string(),
    userId: v.optional(v.id("users")),
    totalDurationS: v.optional(v.number()),
    notes: v.optional(v.string()),
  },
  returns: v.id("sessions"),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    const riderId = args.userId ?? me._id;

    if (me.role === "user") {
      if (riderId !== me._id)
        throw new ConvexError("You can only launch a session for yourself");
      if (!(await holdsLaunchRight(ctx, me, args.machineId))) {
        throw new ConvexError(
          "You have not been given the right to launch sessions on this machine",
        );
      }
    } else {
      if (!(await canAccessMachine(ctx, args.machineId, me))) {
        throw new ConvexError("Not authorized to use this machine");
      }
      if (!(await canAccessUser(ctx, riderId, me))) {
        throw new ConvexError(
          "Not authorized to launch a session for this rider",
        );
      }
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine || machine.isDeleted)
      throw new ConvexError("Machine not found");
    // The rider is an active member of the machine's organisation.
    if (
      (await organizationRoleOf(ctx, riderId, machine.organizationId)) === null
    ) {
      throw new ConvexError(
        "Not authorized to launch a session for this rider",
      );
    }
    if (machine.status === "offline")
      throw new ConvexError("Machine is offline");
    if (machine.status === "in_session")
      throw new ConvexError("Machine is already in a session");
    if (!machine.programsEnabled) {
      throw new ConvexError(
        "This machine does not accept programmed sessions yet (manual only, at the machine)",
      );
    }
    const busy = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .first();
    if (busy)
      throw new ConvexError("A session is already waiting for this machine");

    const profile = await ctx.db
      .query("machine_profiles")
      .withIndex("by_machine_and_profile", (q) =>
        q.eq("machineId", args.machineId).eq("profileId", args.profileId),
      )
      .unique();
    if (!profile) throw new ConvexError("This programme is not on the machine");

    const rider = await ctx.db.get(riderId);
    if (!rider) throw new ConvexError("Rider not found");
    const now = Date.now();
    // Both helpers give null for a value on record that is not a finite whole
    // number: what is compared below, and copied to the session, always is one.
    const hrMax = effectiveHrMax(rider, now);
    if (hrMax === null) {
      throw new ConvexError(
        "The rider's max heart rate (or birth year) must be set by a manager before an auto session",
      );
    }
    const refusal = zoneRefusal(profile, hrMax);
    if (refusal) throw new ConvexError(refusal);
    const age = ageFrom(rider.birthYear, now);
    if (age === null) {
      throw new ConvexError(
        "The rider's birth year must be set by a manager before an auto session",
      );
    }
    if (age < MIN_RIDER_AGE) {
      throw new ConvexError(
        `Rider is ${age}: auto sessions require at least ${MIN_RIDER_AGE} years`,
      );
    }

    if (args.totalDurationS !== undefined) {
      if (!Number.isFinite(args.totalDurationS) || args.totalDurationS <= 0) {
        throw new ConvexError("Duration must be positive");
      }
    }

    return await ctx.db.insert("sessions", {
      organizationId: machine.organizationId,
      machineId: args.machineId,
      userId: riderId,
      startedById: me._id,
      status: "pending",
      startedAt: now,
      channels: ["ECG"],
      notes: args.notes,
      kind: "auto",
      origin: "remote",
      profileId: profile.profileId,
      profileName: profile.name,
      zoneLowBpm: profile.zoneLowBpm,
      zoneHighBpm: profile.zoneHighBpm,
      totalDurationS: args.totalDurationS ?? profile.totalDurationS,
      subjectHrMax: hrMax,
      subjectAge: age,
      subjectLabel: fullName(rider),
      operatorName: fullName(me),
    });
  },
});

/**
 * Ask a training session to stop. Pending: cancelled here. Active: the Pi is
 * asked to stop, and ramps down on the commissioned ramp; the session becomes
 * completed when the Pi reports the end, not before - the arm is still
 * turning until then.
 */
export const requestStop = mutation({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    // A session of another organisation is a session that does not exist.
    const session = await ctx.db.get(args.sessionId);
    if (!session || !inScope(me, session.organizationId))
      throw new ConvexError("Session not found");
    if (!(await canAccessSession(ctx, session, me))) {
      throw new ConvexError("Not authorized to stop this session");
    }
    const now = Date.now();
    if (session.status === "pending") {
      await ctx.db.patch(args.sessionId, {
        status: "failed",
        endedAt: now,
        endReason: `Cancelled before start by ${fullName(me)}`,
      });
      return null;
    }
    if (session.status !== "active") return null;
    if (session.stopRequestedAt === undefined) {
      await ctx.db.patch(args.sessionId, { stopRequestedAt: now });
    }
    return null;
  },
});

// ---------------------------------------------------------------------------
// Live view
// ---------------------------------------------------------------------------

export const getMachineLive = query({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.object({
      status: v.string(),
      programsEnabled: v.boolean(),
      live: v.union(liveStateValidator, v.null()),
      stale: v.boolean(),
      // The server's clock in this answer (see listLaunchableMachines).
      serverNow: v.number(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canSeeMachineTraining(ctx, me, args.machineId))) return null;
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;
    const live = await authorizedMachineLive(ctx, me, machine);
    const now = Date.now();
    return {
      status: machine.status,
      programsEnabled: machine.programsEnabled ?? false,
      live,
      stale: live === null || now - live.updatedAt > LIVE_FRESH_MS,
      serverNow: now,
    };
  },
});

// ---------------------------------------------------------------------------
// Two clocks: the machine's and the server's
// ---------------------------------------------------------------------------

/**
 * No session started before this date: an age that would place a start
 * earlier is not an age, and is not read.
 */
export const EARLIEST_SESSION_START_MS = Date.UTC(2024, 0, 1);

/**
 * How far outside its session a point or an event may be dated and still be
 * stored: one minute before the start the machine dated, one minute after the
 * end.
 */
export const SESSION_WINDOW_MARGIN_MS = 60_000;

/**
 * When a session started, on the server's clock, for a machine that says how
 * long ago it started it (`ageMs`, counted on its monotonic clock): that long
 * before now. The date its wall clock wrote is not read at all.
 *
 * Null when the machine says no age, or one that cannot be: a negative one, or
 * one that would place the start before `notBefore`. The session is then
 * dated as it always was, and has no machine axis (see `machineAxis`).
 */
function startFromAge(
  now: number,
  ageMs: number | undefined,
  notBefore: number,
): number | null {
  if (ageMs === undefined || ageMs < 0) return null;
  const start = now - ageMs;
  return start >= notBefore ? start : null;
}

/**
 * The machine's own time axis for a session: the start as the machine dated
 * it, and what must be added to a date of that axis to place it on the
 * server's clock (`shift`).
 *
 * A session has one only if its machine said how long ago it started it, when
 * it registered it or confirmed its start: `machineStartedAt` is written then,
 * and at no other time. Such a machine dates every point, every event and the
 * end as that start plus the time elapsed, so one shift places them all on
 * the server's clock, where `startedAt` and `endedAt` are.
 *
 * Null for every other session (a console that does not say its age, and
 * every session stored before this rule): nothing is shifted and nothing is
 * bounded, its dates are stored and served as the machine wrote them.
 */
function machineAxis(
  session: Doc<"sessions">,
): { start: number; shift: number } | null {
  const start = session.machineStartedAt;
  return start === undefined
    ? null
    : { start, shift: session.startedAt - start };
}

/**
 * The dates, on the machine's axis, a point or an event of this session may
 * carry: from one minute before the start the machine dated to one minute
 * after the end; no upper bound while the session has not ended. The server's
 * own clock is not compared with any of them.
 *
 * No bound at all for a session without a machine axis: its machine dates
 * each point by reading its wall clock, which may be corrected, forwards or
 * backwards, in the middle of the session. Bounding those dates would refuse
 * what was measured.
 */
function sessionWindow(session: Doc<"sessions">): {
  from: number | null;
  to: number | null;
} {
  const axis = machineAxis(session);
  if (axis === null) return { from: null, to: null };
  return {
    from: axis.start - SESSION_WINDOW_MARGIN_MS,
    to:
      session.endedAt === undefined
        ? null
        : session.endedAt - axis.shift + SESSION_WINDOW_MARGIN_MS,
  };
}

function inWindow(
  window: { from: number | null; to: number | null },
  t: number,
): boolean {
  return (
    (window.from === null || t >= window.from) &&
    (window.to === null || t <= window.to)
  );
}

const telemetryPoint = v.object({
  t: v.number(),
  elapsedS: v.number(),
  phase: v.string(),
  bpm: v.optional(v.number()),
  motorRpm: v.number(),
  outputRpm: v.number(),
  setpointMotorRpm: v.number(),
  gLoad: v.number(),
  safetyAction: v.string(),
});

/**
 * Telemetry for a session, oldest first; `sinceT` for incremental reads.
 *
 * `t` is served on the server's clock (see `machineAxis`), and `sinceT` is
 * read on that same clock: a machine whose clock is wrong draws the same
 * curve, at the same dates, as one on time.
 */
export const getSessionTelemetry = query({
  args: {
    sessionId: v.id("sessions"),
    sinceT: v.optional(v.number()),
    limit: v.optional(v.number()),
  },
  returns: v.array(telemetryPoint),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    const session = await ctx.db.get(args.sessionId);
    if (!session) return [];
    if (!(await canAccessSession(ctx, session, me))) return [];
    const limit = Math.min(Math.max(args.limit ?? 3600, 1), 7200);
    const shift = machineAxis(session)?.shift ?? 0;
    const sinceT = args.sinceT;
    const rows = await ctx.db
      .query("training_telemetry")
      .withIndex("by_session_and_t", (q) =>
        sinceT === undefined
          ? q.eq("sessionId", args.sessionId)
          : q.eq("sessionId", args.sessionId).gt("t", sinceT - shift),
      )
      .order("desc")
      .take(limit);
    return rows.reverse().map((r) => ({
      t: r.t + shift,
      elapsedS: r.elapsedS,
      phase: r.phase,
      bpm: r.bpm,
      motorRpm: r.motorRpm,
      outputRpm: r.outputRpm,
      setpointMotorRpm: r.setpointMotorRpm,
      gLoad: r.gLoad,
      safetyAction: r.safetyAction,
    }));
  },
});

/**
 * The last sign of life of an active session: two dates of one telemetry
 * point, the one with the greatest `t`, which is the point the dashboard
 * shows as the session's current values.
 *
 * - `lastSignalAt`: when the server received it (the date the server gave the
 *   row, not the `t` the machine wrote in it). While no point has arrived, the
 *   start of the session as the server dated it: a session the machine
 *   registered itself carries the machine's own start date, so the server's
 *   date for it is the creation of its row.
 * - `lastMeasuredAt`: when it was measured: its `t`, placed on the server's
 *   clock (see `machineAxis`). A point received this instant may have been
 *   measured long ago: the machine sends again, oldest first, what it recorded
 *   during a link loss or before a restart. Null while no point has arrived.
 *
 * Both are null for a session that is not active.
 */
async function lastSignal(
  ctx: QueryCtx,
  session: Doc<"sessions">,
): Promise<{ lastSignalAt: number | null; lastMeasuredAt: number | null }> {
  if (session.status !== "active") {
    return { lastSignalAt: null, lastMeasuredAt: null };
  }
  const latest = await ctx.db
    .query("training_telemetry")
    .withIndex("by_session_and_t", (q) => q.eq("sessionId", session._id))
    .order("desc")
    .first();
  if (latest) {
    return {
      lastSignalAt: Math.floor(latest._creationTime),
      lastMeasuredAt: latest.t + (machineAxis(session)?.shift ?? 0),
    };
  }
  return {
    lastSignalAt:
      session.origin === "local"
        ? Math.floor(session._creationTime)
        : session.startedAt,
    lastMeasuredAt: null,
  };
}

/** The training fields of one session, for the live and detail pages. */
export const getTrainingSession = query({
  args: { sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      _id: v.id("sessions"),
      machineId: v.id("machines"),
      machineName: v.string(),
      status: v.string(),
      kind: v.string(),
      origin: v.optional(v.string()),
      profileId: v.optional(v.string()),
      profileName: v.optional(v.string()),
      zoneLowBpm: v.optional(v.number()),
      zoneHighBpm: v.optional(v.number()),
      totalDurationS: v.optional(v.number()),
      subjectHrMax: v.optional(v.number()),
      subjectLabel: v.optional(v.string()),
      operatorName: v.optional(v.string()),
      startedAt: v.number(),
      // The last sign of life of an active session (see `lastSignal`): when
      // the server received its latest point, when the machine says it
      // measured it, and the server's clock in this answer. The dashboard
      // shows a value as current only if both dates are recent on that clock.
      lastSignalAt: v.union(v.number(), v.null()),
      lastMeasuredAt: v.union(v.number(), v.null()),
      serverNow: v.number(),
      endedAt: v.optional(v.number()),
      stopRequestedAt: v.optional(v.number()),
      endReason: v.optional(v.string()),
      canStop: v.boolean(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    const s = await ctx.db.get(args.sessionId);
    if (!s) return null;
    // Whoever may read the session may stop it: its rider, or whoever can
    // access its machine, in the session's organisation.
    if (!(await canAccessSession(ctx, s, me))) return null;
    const machine = await ctx.db.get(s.machineId);
    return {
      _id: s._id,
      machineId: s.machineId,
      machineName: machine?.name ?? "-",
      status: s.status,
      kind: s.kind ?? "recording",
      origin: s.origin,
      profileId: s.profileId,
      profileName: s.profileName,
      zoneLowBpm: s.zoneLowBpm,
      zoneHighBpm: s.zoneHighBpm,
      totalDurationS: s.totalDurationS,
      subjectHrMax: s.subjectHrMax,
      subjectLabel: s.subjectLabel,
      operatorName: s.operatorName,
      startedAt: s.startedAt,
      ...(await lastSignal(ctx, s)),
      serverNow: Date.now(),
      endedAt: s.endedAt,
      stopRequestedAt: s.stopRequestedAt,
      endReason: s.endReason,
      canStop: s.status === "pending" || s.status === "active",
    };
  },
});

// ---------------------------------------------------------------------------
// Internal: what the Pi's HTTP routes call
// ---------------------------------------------------------------------------

export const updateLive = internalMutation({
  args: {
    machineId: v.id("machines"),
    live: liveStateValidator,
    programsEnabled: v.optional(v.boolean()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const patch: Partial<Doc<"machines">> = { live: args.live };
    if (args.programsEnabled !== undefined)
      patch.programsEnabled = args.programsEnabled;
    await ctx.db.patch(args.machineId, patch);
    return null;
  },
});

export const syncProfiles = internalMutation({
  args: {
    machineId: v.id("machines"),
    storeRev: v.number(),
    programsEnabled: v.boolean(),
    profiles: v.array(profileValidator),
  },
  returns: v.object({ count: v.number() }),
  handler: async (ctx, args) => {
    // What a machine writes inherits the machine's organisation.
    const machine = await ctx.db.get(args.machineId);
    if (!machine) throw machineError("machine_not_found", "Machine not found");
    const existing = await ctx.db
      .query("machine_profiles")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    for (const row of existing) await ctx.db.delete(row._id);
    const now = Date.now();
    for (const p of args.profiles) {
      await ctx.db.insert("machine_profiles", {
        organizationId: machine.organizationId,
        machineId: args.machineId,
        ...p,
        storeRev: args.storeRev,
        updatedAt: now,
      });
    }
    await ctx.db.patch(args.machineId, {
      programsEnabled: args.programsEnabled,
    });
    return { count: args.profiles.length };
  },
});

/**
 * Riders the local panel may pick from: users holding the launch right who are
 * still active members of the machine's organisation.
 */
export const getRoster = internalQuery({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      userId: v.id("users"),
      name: v.string(),
      hrMax: v.union(v.number(), v.null()),
    }),
  ),
  handler: async (ctx, args) => {
    const rows = await ctx.db
      .query("machine_user_permissions")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return [];
    const now = Date.now();
    const result = [];
    for (const row of rows) {
      const role = await organizationRoleOf(
        ctx,
        row.userId,
        machine.organizationId,
      );
      const user = role === null ? null : await ctx.db.get(row.userId);
      if (!user) continue;
      result.push({
        userId: user._id,
        name: fullName(user),
        hrMax: effectiveHrMax(user, now),
      });
    }
    return result.sort((a, b) => a.name.localeCompare(b.name));
  },
});

export const getPendingTrainingSession = internalQuery({
  args: { machineId: v.id("machines") },
  returns: v.union(
    v.object({
      sessionId: v.id("sessions"),
      profileId: v.string(),
      totalDurationS: v.union(v.number(), v.null()),
      subjectId: v.string(),
      subjectLabel: v.string(),
      subjectHrMax: v.number(),
      subjectAge: v.union(v.number(), v.null()),
      operatorName: v.string(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const pending = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", args.machineId).eq("status", "pending"),
      )
      .collect();
    const s = pending.find((p) => p.kind === "auto");
    if (
      !s ||
      s.profileId === undefined ||
      s.subjectHrMax === undefined ||
      s.userId === undefined
    ) {
      return null;
    }
    return {
      sessionId: s._id,
      profileId: s.profileId,
      totalDurationS: s.totalDurationS ?? null,
      subjectId: s.userId,
      subjectLabel: s.subjectLabel ?? "",
      subjectHrMax: s.subjectHrMax,
      subjectAge: s.subjectAge ?? null,
      operatorName: s.operatorName ?? "dashboard",
    };
  },
});

/**
 * The machine armed a launch from the dashboard: the session starts at the
 * reception of this call.
 *
 * Unless the machine says both the start as it dated it (`machineStartedAt`)
 * and how long ago that was on its monotonic clock (`sessionAgeMs`): the
 * start is then that long before now, and the session has a machine axis. An
 * age that would place the start before the launch itself is not read.
 */
export const markTrainingStarted = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    machineStartedAt: v.optional(v.number()),
    sessionAgeMs: v.optional(v.number()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    if (s.status !== "pending")
      throw machineError(
        "session_not_pending",
        `Session is not pending (status: ${s.status})`,
      );
    const now = Date.now();
    // While pending, `startedAt` is the date of the launch.
    const start =
      args.machineStartedAt === undefined
        ? null
        : startFromAge(now, args.sessionAgeMs, s.startedAt);
    await ctx.db.patch(
      args.sessionId,
      start === null
        ? { status: "active", startedAt: now }
        : {
            status: "active",
            startedAt: start,
            machineStartedAt: args.machineStartedAt,
          },
    );
    await ctx.db.patch(args.machineId, { status: "in_session" });
    return null;
  },
});

/**
 * A session the Pi started at the machine (manual, or auto from the panel).
 * Idempotent: one session per `localRef`, dated once, at its first call.
 *
 * `startedAt` is the start as the machine dated it, and it is the session's
 * `startedAt`, as it always was.
 *
 * Unless the machine also says how long ago it started the session, on its
 * monotonic clock (`sessionAgeMs`): the session's `startedAt` is then that
 * long before now, on the server's clock, the date the machine wrote is kept
 * as `machineStartedAt`, and the session has a machine axis. An age that
 * would place the start before `EARLIEST_SESSION_START_MS` is not read.
 */
export const registerLocalSession = internalMutation({
  args: {
    machineId: v.id("machines"),
    localRef: v.string(),
    kind: v.union(v.literal("auto"), v.literal("manual")),
    startedAt: v.number(),
    sessionAgeMs: v.optional(v.number()),
    operatorName: v.string(),
    userId: v.optional(v.string()),
    subjectLabel: v.optional(v.string()),
    profileId: v.optional(v.string()),
    profileName: v.optional(v.string()),
    zoneLowBpm: v.optional(v.number()),
    zoneHighBpm: v.optional(v.number()),
    totalDurationS: v.optional(v.number()),
    subjectHrMax: v.optional(v.number()),
    occupancy: v.optional(v.string()),
  },
  returns: v.id("sessions"),
  handler: async (ctx, args) => {
    const existing = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_local_ref", (q) =>
        q.eq("machineId", args.machineId).eq("localRef", args.localRef),
      )
      .unique();
    if (existing) return existing._id;
    const machine = await ctx.db.get(args.machineId);
    if (!machine) throw machineError("machine_not_found", "Machine not found");
    const claimedRider = args.userId
      ? ctx.db.normalizeId("users", args.userId)
      : null;
    // The rider the machine names is kept only if they are an active member
    // of the machine's organisation; the session is recorded either way.
    const userId =
      claimedRider &&
      (await organizationRoleOf(ctx, claimedRider, machine.organizationId)) !==
        null
        ? claimedRider
        : null;
    const start = startFromAge(
      Date.now(),
      args.sessionAgeMs,
      EARLIEST_SESSION_START_MS,
    );
    const id = await ctx.db.insert("sessions", {
      organizationId: machine.organizationId,
      machineId: args.machineId,
      userId: userId ?? undefined,
      status: "active",
      startedAt: start ?? args.startedAt,
      machineStartedAt: start === null ? undefined : args.startedAt,
      channels: ["ECG"],
      notes: args.occupancy ? `Occupancy: ${args.occupancy}` : undefined,
      kind: args.kind,
      origin: "local",
      localRef: args.localRef,
      profileId: args.profileId,
      profileName: args.profileName,
      zoneLowBpm: args.zoneLowBpm,
      zoneHighBpm: args.zoneHighBpm,
      totalDurationS: args.totalDurationS,
      subjectHrMax: args.subjectHrMax,
      subjectLabel: args.subjectLabel,
      operatorName: args.operatorName,
    });
    await ctx.db.patch(args.machineId, { status: "in_session" });
    return id;
  },
});

export const endTrainingSession = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    failed: v.boolean(),
    reason: v.string(),
    endedAt: v.optional(v.number()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    if (s.status === "completed" || s.status === "failed") return null; // idempotent
    // The end as the machine dated it, or now when it dated none. Where the
    // session has a machine axis, the machine's date is placed on the
    // server's clock, and never before the start.
    const axis = machineAxis(s);
    let endedAt = args.endedAt ?? Date.now();
    if (axis !== null && args.endedAt !== undefined) {
      endedAt = Math.max(s.startedAt, args.endedAt + axis.shift);
    }
    await ctx.db.patch(args.sessionId, {
      status: args.failed ? "failed" : "completed",
      endedAt,
      endReason: args.reason,
    });
    await ctx.db.patch(args.machineId, { status: "online" });
    // Nothing is scheduled here: the summary of a training session will be
    // built from its record (ANH-89), not from ECG batches.
    return null;
  },
});

export const getTrainingStatus = internalQuery({
  args: { machineId: v.id("machines"), sessionId: v.id("sessions") },
  returns: v.union(
    v.object({
      status: v.string(),
      active: v.boolean(),
      stopRequested: v.boolean(),
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId) return null;
    return {
      status: s.status,
      active: s.status === "active",
      stopRequested: s.stopRequestedAt !== undefined,
    };
  },
});

/** What the machine is told of a batch: every item is in exactly one count. */
const batchOutcome = v.object({
  /** Stored by this call. */
  stored: v.number(),
  /** Already stored by an earlier call: left as they were. */
  duplicates: v.number(),
  /** Dated outside the session (see `sessionWindow`): not stored. */
  rejected: v.number(),
});

/**
 * Store telemetry points of a session of this machine. Idempotent: a point is
 * one `(sessionId, t)`, and a point already stored is left as it is, with the
 * date the server first received it. The machine may therefore send a batch
 * again whenever it is not sure it was received (a lost answer, a restart).
 *
 * For a session that has a machine axis, a point dated outside the session
 * is counted and not stored; it does not refuse the batch, whose other points
 * are stored. For any other session no date is refused.
 */
export const storeTelemetry = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    points: v.array(telemetryPoint),
  },
  returns: batchOutcome,
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    const window = sessionWindow(s);
    const outcome = { stored: 0, duplicates: 0, rejected: 0 };
    for (const p of args.points) {
      if (!inWindow(window, p.t)) {
        outcome.rejected++;
        continue;
      }
      // `first`, not `unique`: rows written before this rule may hold the
      // same point twice.
      const known = await ctx.db
        .query("training_telemetry")
        .withIndex("by_session_and_t", (q) =>
          q.eq("sessionId", args.sessionId).eq("t", p.t),
        )
        .first();
      if (known) {
        outcome.duplicates++;
        continue;
      }
      await ctx.db.insert("training_telemetry", {
        organizationId: s.organizationId,
        sessionId: args.sessionId,
        machineId: args.machineId,
        ...p,
      });
      outcome.stored++;
    }
    return outcome;
  },
});

const trainingEvent = v.object({
  seq: v.number(),
  t: v.number(),
  kind: v.string(),
  detail: v.string(),
  actor: v.string(),
});

/**
 * Store events of a session of this machine, as it reads them back from the
 * session's local record. Idempotent like `storeTelemetry`: an event is one
 * `(sessionId, seq)`, its rank in that record, and an event already stored is
 * left as it is. The dates are bounded as those of the telemetry are.
 */
export const storeEvents = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    events: v.array(trainingEvent),
  },
  returns: batchOutcome,
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    const window = sessionWindow(s);
    const outcome = { stored: 0, duplicates: 0, rejected: 0 };
    for (const event of args.events) {
      if (!inWindow(window, event.t)) {
        outcome.rejected++;
        continue;
      }
      const known = await ctx.db
        .query("training_events")
        .withIndex("by_session_and_seq", (q) =>
          q.eq("sessionId", args.sessionId).eq("seq", event.seq),
        )
        .first();
      if (known) {
        outcome.duplicates++;
        continue;
      }
      await ctx.db.insert("training_events", {
        organizationId: s.organizationId,
        sessionId: args.sessionId,
        machineId: args.machineId,
        ...event,
      });
      outcome.stored++;
    }
    return outcome;
  },
});
