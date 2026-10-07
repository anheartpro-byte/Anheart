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
  canAccessUser,
  canManageMachine,
  getCurrentUserOrThrow,
} from "./lib/auth";
import { liveStateValidator, machineProfileFields } from "./schema";
import { authorizedMachineLive } from "./lib/trainingPrivacy";
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

/** Measured maximum if known, else the Tanaka estimate (208 - 0.7 x age). */
export function effectiveHrMax(
  user: Pick<Doc<"users">, "hrMax" | "birthYear">,
  now: number,
): number | null {
  if (user.hrMax !== undefined) {
    return user.hrMax >= HR_MAX_MIN && user.hrMax <= HR_MAX_MAX
      ? user.hrMax
      : null;
  }
  if (user.birthYear === undefined) return null;
  const age = new Date(now).getUTCFullYear() - user.birthYear;
  if (age < MIN_AGE || age > MAX_AGE) return null;
  return Math.round(208 - 0.7 * age);
}

/** Why a programme is unsafe for this rider, or null if it is acceptable. */
export function zoneRefusal(
  profile: { zoneHighBpm: number; hardMaxBpm: number },
  hrMax: number,
): string | null {
  const ceiling = Math.floor(ZONE_CEILING_FRACTION * hrMax);
  if (profile.zoneHighBpm > ceiling) {
    return `Zone up to ${profile.zoneHighBpm} bpm exceeds 90% of this rider's max heart rate (${hrMax} bpm → ceiling ${ceiling} bpm)`;
  }
  if (profile.hardMaxBpm > hrMax) {
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

/** Age in whole years from a birth year (the later birthday assumed: never older). */
export function ageFrom(
  birthYear: number | undefined,
  now: number,
): number | null {
  if (birthYear === undefined) return null;
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

/** Admin, gestionnaire of the machine, or a user holding the launch right. */
async function canSeeMachineTraining(
  ctx: QueryCtx | MutationCtx,
  user: Doc<"users">,
  machineId: Id<"machines">,
): Promise<boolean> {
  if (await canAccessMachine(ctx, machineId)) return true;
  return (
    user.role === "user" && (await hasLaunchRight(ctx, machineId, user._id))
  );
}

function fullName(user: Pick<Doc<"users">, "firstName" | "lastName">) {
  return `${user.firstName} ${user.lastName}`.trim();
}

export const grantLaunchRight = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId))) {
      throw new ConvexError(
        "Only an admin or a manager of this machine can grant launch rights",
      );
    }
    const target = await ctx.db.get(args.userId);
    if (!target) throw new ConvexError("User not found");
    if (target.role !== "user") {
      throw new ConvexError(
        "Launch rights are granted to users; managers and admins already have them",
      );
    }
    // A gestionnaire may only grant rights to a patient they manage.
    if (!(await canAccessUser(ctx, args.userId))) {
      throw new ConvexError("You do not manage this user");
    }
    if (await hasLaunchRight(ctx, args.machineId, args.userId)) return null;
    await ctx.db.insert("machine_user_permissions", {
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
    await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId))) {
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
    await getCurrentUserOrThrow(ctx);
    if (!(await canAccessMachine(ctx, args.machineId))) return [];
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
    if (!(await canAccessUser(ctx, args.userId))) {
      throw new ConvexError("You do not manage this user");
    }
    const patch: Partial<Pick<Doc<"users">, "hrMax" | "birthYear">> = {};
    if (args.hrMax !== undefined) {
      if (
        args.hrMax !== null &&
        (args.hrMax < HR_MAX_MIN || args.hrMax > HR_MAX_MAX)
      ) {
        throw new ConvexError(
          `Max heart rate must be within ${HR_MAX_MIN}-${HR_MAX_MAX} bpm`,
        );
      }
      patch.hrMax = args.hrMax ?? undefined;
    }
    if (args.birthYear !== undefined) {
      const year = new Date().getUTCFullYear();
      if (
        args.birthYear !== null &&
        (year - args.birthYear < MIN_AGE || year - args.birthYear > MAX_AGE)
      ) {
        throw new ConvexError("Birth year gives an implausible age");
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
 * state and presets. Admin: every machine. Gestionnaire: the ones they manage.
 * User: the ones they hold the launch right for.
 */
export const listLaunchableMachines = query({
  args: {},
  returns: v.array(
    v.object({
      _id: v.id("machines"),
      name: v.string(),
      location: v.optional(v.string()),
      status: v.string(),
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
      result.push({
        _id: machine._id,
        name: machine.name,
        location: machine.location,
        status: machine.status,
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
      if (!(await hasLaunchRight(ctx, args.machineId, me._id))) {
        throw new ConvexError(
          "You have not been given the right to launch sessions on this machine",
        );
      }
    } else {
      if (!(await canAccessMachine(ctx, args.machineId))) {
        throw new ConvexError("Not authorized to use this machine");
      }
      if (!(await canAccessUser(ctx, riderId))) {
        throw new ConvexError(
          "Not authorized to launch a session for this rider",
        );
      }
    }

    const machine = await ctx.db.get(args.machineId);
    if (!machine || machine.isDeleted)
      throw new ConvexError("Machine not found");
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
    const session = await ctx.db.get(args.sessionId);
    if (!session) throw new ConvexError("Session not found");
    const isRider = session.userId === me._id;
    if (!isRider && !(await canAccessMachine(ctx, session.machineId))) {
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
    }),
    v.null(),
  ),
  handler: async (ctx, args) => {
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canSeeMachineTraining(ctx, me, args.machineId))) return null;
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return null;
    const live = await authorizedMachineLive(ctx, me, machine);
    return {
      status: machine.status,
      programsEnabled: machine.programsEnabled ?? false,
      live,
      stale: live === null || Date.now() - live.updatedAt > LIVE_FRESH_MS,
    };
  },
});

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

/** Telemetry for a session, oldest first; `sinceT` for incremental reads. */
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
    const isRider = session.userId === me._id;
    if (!isRider && !(await canAccessMachine(ctx, session.machineId)))
      return [];
    const limit = Math.min(Math.max(args.limit ?? 3600, 1), 7200);
    const rows = await ctx.db
      .query("training_telemetry")
      .withIndex("by_session_and_t", (q) =>
        args.sinceT === undefined
          ? q.eq("sessionId", args.sessionId)
          : q.eq("sessionId", args.sessionId).gt("t", args.sinceT),
      )
      .order("desc")
      .take(limit);
    return rows.reverse().map((r) => ({
      t: r.t,
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
    const isRider = s.userId === me._id;
    const onMachine = await canAccessMachine(ctx, s.machineId);
    if (!isRider && !onMachine) return null;
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
      endedAt: s.endedAt,
      stopRequestedAt: s.stopRequestedAt,
      endReason: s.endReason,
      canStop:
        (s.status === "pending" || s.status === "active") &&
        (isRider || onMachine),
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
    const existing = await ctx.db
      .query("machine_profiles")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    for (const row of existing) await ctx.db.delete(row._id);
    const now = Date.now();
    for (const p of args.profiles) {
      await ctx.db.insert("machine_profiles", {
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

/** Riders the local panel may pick from: users holding the launch right. */
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
    const now = Date.now();
    const result = [];
    for (const row of rows) {
      const user = await ctx.db.get(row.userId);
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

export const markTrainingStarted = internalMutation({
  args: { machineId: v.id("machines"), sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw new ConvexError("Session not found");
    if (s.status !== "pending")
      throw new ConvexError(`Session is not pending (status: ${s.status})`);
    await ctx.db.patch(args.sessionId, {
      status: "active",
      startedAt: Date.now(),
    });
    await ctx.db.patch(args.machineId, { status: "in_session" });
    return null;
  },
});

/** A session the Pi started at the machine (manual, or auto from the panel). Idempotent. */
export const registerLocalSession = internalMutation({
  args: {
    machineId: v.id("machines"),
    localRef: v.string(),
    kind: v.union(v.literal("auto"), v.literal("manual")),
    startedAt: v.number(),
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
    if (!machine) throw new ConvexError("Machine not found");
    const userId = args.userId
      ? ctx.db.normalizeId("users", args.userId)
      : null;
    const id = await ctx.db.insert("sessions", {
      machineId: args.machineId,
      userId: userId ?? undefined,
      status: "active",
      startedAt: args.startedAt,
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
      throw new ConvexError("Session not found");
    if (s.status === "completed" || s.status === "failed") return null; // idempotent
    await ctx.db.patch(args.sessionId, {
      status: args.failed ? "failed" : "completed",
      endedAt: args.endedAt ?? Date.now(),
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

export const storeTelemetry = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    points: v.array(telemetryPoint),
  },
  returns: v.object({ stored: v.number() }),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw new ConvexError("Session not found");
    for (const p of args.points) {
      await ctx.db.insert("training_telemetry", {
        sessionId: args.sessionId,
        machineId: args.machineId,
        ...p,
      });
    }
    return { stored: args.points.length };
  },
});
