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
 *
 * Only the machine ends a session it started. A session of which the server
 * has had no news for `SESSION_SILENCE_MS` is marked `interrupted` (end
 * unknown), never `completed` nor `failed`: the machine is not told to stop
 * it, a heartbeat that names it makes it active again, and the end the
 * machine sends, however late, closes it (`interruptSilentSessions`).
 *
 * A launch is checked again when the machine arms it (ANH-144), not only when
 * it is made: whoever made it can still make it, the rider still may ride,
 * the machine is the one it was, and the launch has not waited longer than
 * `PENDING_SESSION_TTL_S` (`armingRefusal`). One that fails becomes a failed
 * session with a stable code (`endCode`), the machine's start is answered 409
 * with that code, and the poll never offers it. A rider has one session at a
 * time, on any machine, and a machine one launch at a time (`launchAutoSession`).
 */
import { limitUser } from "./lib/rateLimits";
import { v } from "convex/values";
import {
  internalMutation,
  internalQuery,
  mutation,
  query,
  type MutationCtx,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import { internal } from "./_generated/api";
import {
  canAccessMachine,
  canAccessSession,
  canAccessUser,
  canManageMachine,
  getCurrentUserOrThrow,
  inScope,
  organizationRoleOf,
  requireSharedProfileAuthority,
  rolesHeldNow,
  sameOrganization,
  type CurrentUser,
} from "./lib/auth";
import { liveStateValidator, machineProfileFields } from "./schema";
import { authorizedMachineLive } from "./lib/trainingPrivacy";
import { machineError, type MachineErrorCode } from "./lib/contract";
// The stable codes of the refusals the site words (ANH-90, ANH-144).
import { refusal, type DashboardErrorCode } from "./lib/errors";
// A heartbeat older than this means the machine cannot be relied on to answer.
// The dashboard judges freshness on the same value (hooks/use-freshness.ts).
import { LIVE_FRESH_MS, SESSION_SILENCE_MS } from "../lib/training";
// The alerts to the gestionnaires that a machine's report may call for.
import {
  driveFaultAlerts,
  driveFaultCode,
  foldedDetail,
  scheduleAlert,
  verdictAction,
  verdictAlerts,
} from "./lib/notifications";

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
 * Why a programme is unsafe for this rider, or null if it is acceptable: the
 * stable code the launch is refused with (`convex/lib/errors.ts`). The site
 * shows the values themselves before the launch (`LaunchTrainingModal`).
 *
 * Each rule is written as what is accepted, negated: a programme passes only
 * if its value is known to be at or under the limit. A value that is not a
 * number on either side is therefore a refusal.
 */
export function zoneRefusal(
  profile: { zoneHighBpm: number; hardMaxBpm: number },
  hrMax: number,
): "zone_above_ceiling" | "hard_max_above_hr_max" | null {
  const ceiling = Math.floor(ZONE_CEILING_FRACTION * hrMax);
  if (!(profile.zoneHighBpm <= ceiling)) return "zone_above_ceiling";
  if (!(profile.hardMaxBpm <= hrMax)) return "hard_max_above_hr_max";
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

/**
 * Why `launcher` may not launch a session for `riderId` on this machine, as
 * the stable code the launch is refused with, or null when they may: a user
 * for themselves, holding the launch right; an admin or a gestionnaire on a
 * machine they reach, for a rider they manage (or themselves). The rule of
 * `launchAutoSession`, and the one its launch is checked against again when
 * the machine arms it (`armingRefusal`).
 */
async function launcherRefusal(
  ctx: QueryCtx | MutationCtx,
  launcher: CurrentUser,
  riderId: Id<"users">,
  machineId: Id<"machines">,
): Promise<DashboardErrorCode | null> {
  if (launcher.role === "user") {
    if (riderId !== launcher._id) return "launch_for_self_only";
    return (await holdsLaunchRight(ctx, launcher, machineId))
      ? null
      : "launch_right_missing";
  }
  if (!(await canAccessMachine(ctx, machineId, launcher))) {
    return "machine_not_managed";
  }
  return (await canAccessUser(ctx, riderId, launcher))
    ? null
    : "user_not_managed";
}

function fullName(user: Pick<Doc<"users">, "firstName" | "lastName">) {
  return `${user.firstName} ${user.lastName}`.trim();
}

export const grantLaunchRight = mutation({
  args: { machineId: v.id("machines"), userId: v.id("users") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId, me))) {
      throw refusal("machine_not_managed");
    }
    const machine = await ctx.db.get(args.machineId);
    if (!machine) throw refusal("machine_not_found");
    // The right goes to a user of the machine's organisation.
    const targetRole = await organizationRoleOf(
      ctx,
      args.userId,
      machine.organizationId,
    );
    if (targetRole !== "user") {
      throw refusal("launch_right_patients_only");
    }
    // A gestionnaire may only grant rights to a patient they manage.
    if (!(await canAccessUser(ctx, args.userId, me))) {
      throw refusal("user_not_managed");
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
    await limitUser(ctx);
    const me = await getCurrentUserOrThrow(ctx);
    if (!(await canManageMachine(ctx, args.machineId, me))) {
      throw refusal("machine_not_managed");
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
    const machine = await ctx.db.get(args.machineId);
    if (!machine) return [];
    const rows = await ctx.db
      .query("machine_user_permissions")
      .withIndex("by_machine", (q) => q.eq("machineId", args.machineId))
      .collect();
    const now = Date.now();
    const result = [];
    for (const row of rows) {
      if (
        (await organizationRoleOf(ctx, row.userId, machine.organizationId)) ===
        null
      )
        continue;
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
    await limitUser(ctx);
    const me = await getCurrentUserOrThrow(ctx);
    if (me.role === "user") throw refusal("not_authorized");
    if (!(await canAccessUser(ctx, args.userId, me))) {
      throw refusal("user_not_managed");
    }
    await requireSharedProfileAuthority(ctx, args.userId, me);
    // Each value is a whole number first, then within its range; null clears
    // it. Both are checked before anything is written.
    const patch: Partial<Pick<Doc<"users">, "hrMax" | "birthYear">> = {};
    if (args.hrMax !== undefined) {
      if (args.hrMax !== null) {
        if (
          !isWholeNumber(args.hrMax) ||
          args.hrMax < HR_MAX_MIN ||
          args.hrMax > HR_MAX_MAX
        ) {
          throw refusal("invalid_hr_max");
        }
      }
      patch.hrMax = args.hrMax ?? undefined;
    }
    if (args.birthYear !== undefined) {
      if (args.birthYear !== null) {
        if (!isWholeNumber(args.birthYear)) {
          throw refusal("invalid_birth_year");
        }
        const age = new Date().getUTCFullYear() - args.birthYear;
        if (age < MIN_AGE || age > MAX_AGE) {
          throw refusal("invalid_birth_year");
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
 * Whether the machine started the session and the server has not received
 * its end: `active`, or `interrupted` (no news of it for a while; it may
 * still be running).
 */
function startedNotEnded(status: Doc<"sessions">["status"]): boolean {
  return status === "active" || status === "interrupted";
}

// ---------------------------------------------------------------------------
// A launch waits, then is checked again when the machine arms it (ANH-144)
// ---------------------------------------------------------------------------

/** How long a launch waits for its machine, unless the deployment says otherwise. */
export const DEFAULT_PENDING_SESSION_TTL_S = 300;
/**
 * The shortest and the longest wait `PENDING_SESSION_TTL_S` may set. The
 * shortest leaves a launch offered for `POLL_MARGIN_S` at least.
 */
export const PENDING_SESSION_TTL_BOUNDS_S = { min: 90, max: 600 } as const;

/**
 * How long before its expiry a launch is no longer offered to its machine.
 * A console of today arms a launch as soon as it reads it, and confirms the
 * start afterwards: one read just before the expiry would start, then be
 * refused at its confirmation and stopped at once. The confirmation itself
 * is accepted until the expiry.
 */
export const POLL_MARGIN_S = 45;

/**
 * How long a launch from the dashboard may wait for its machine to arm it, in
 * ms: the deployment's `PENDING_SESSION_TTL_S` (whole seconds, within
 * `PENDING_SESSION_TTL_BOUNDS_S`), else 300 s. A value that is not one is not
 * read, and the default applies.
 */
export function pendingSessionTtlMs(): number {
  const raw = (process.env.PENDING_SESSION_TTL_S ?? "").trim();
  const seconds = Number(raw);
  const { min, max } = PENDING_SESSION_TTL_BOUNDS_S;
  return /^\d+$/.test(raw) && seconds >= min && seconds <= max
    ? seconds * 1000
    : DEFAULT_PENDING_SESSION_TTL_S * 1000;
}

/** Written as `endReason` on a launch that waited too long. */
export const EXPIRED_REASON = "expired before the machine armed it";

/**
 * Why a launch is refused when its machine arms it: each one a stable code
 * of the machine API (contracts/machine-api.json, `arming_refusals`) and of
 * the dashboard's table (convex/lib/errors.ts, which the site translates),
 * stored on the failed session as `endCode`, with the sentence stored as its
 * `endReason`. In the order they are checked.
 */
export const ARMING_REFUSALS = {
  expired_before_start: EXPIRED_REASON,
  machine_deleted_before_start: "machine deleted before it armed the launch",
  organization_changed_before_start:
    "the machine left the session's organisation before it armed the launch",
  right_revoked_before_start:
    "launch right withdrawn before the machine armed it",
  age_refused_before_start: "the rider's age no longer allows an auto session",
  hr_max_changed_before_start:
    "the rider's max heart rate changed before the machine armed the launch",
  rider_busy:
    "the rider was already in another session when the machine armed the launch",
} as const satisfies Partial<
  Record<Extract<MachineErrorCode, DashboardErrorCode>, string>
>;

export type ArmingRefusalCode = keyof typeof ARMING_REFUSALS;

export const ARMING_REFUSAL_CODES = Object.keys(
  ARMING_REFUSALS,
) as ArmingRefusalCode[];

function isArmingRefusalCode(code: unknown): code is ArmingRefusalCode {
  return ARMING_REFUSAL_CODES.some((known) => known === code);
}

const armingRefusalCode = v.union(
  ...ARMING_REFUSAL_CODES.map((code) => v.literal(code)),
);

type ArmingRefusal = { code: ArmingRefusalCode; message: string };

function refusalFor(code: ArmingRefusalCode): ArmingRefusal {
  return { code, message: ARMING_REFUSALS[code] };
}

/** A launch the machine has not armed `pendingSessionTtlMs` after it was made. */
function expired(session: Doc<"sessions">, now: number): boolean {
  // While pending, `startedAt` is the date of the launch, on the server's clock.
  return now - session.startedAt > pendingSessionTtlMs();
}

/** A launch that will expire within `POLL_MARGIN_S`: no longer offered. */
function expiresSoon(session: Doc<"sessions">, now: number): boolean {
  return now - session.startedAt > pendingSessionTtlMs() - POLL_MARGIN_S * 1000;
}

/**
 * Whether a training session may still hold its rider and its machine: a
 * launch waiting for its machine (and not expired), a session running, or one
 * interrupted while its machine has said nothing since: it may still be
 * running. An interrupted session whose machine has been heard since is not
 * running there, since a heartbeat naming it would have made it active again.
 * A session of the retired recording mode (no `kind`) is history, and holds
 * no rider (see `machineHeld` for its machine).
 */
async function mayHold(
  ctx: QueryCtx | MutationCtx,
  session: Doc<"sessions">,
  now: number,
): Promise<boolean> {
  if (session.kind === undefined) return false;
  if (session.status === "pending") return !expired(session, now);
  if (session.status === "active") return true;
  if (session.status !== "interrupted") return false;
  const machine = await ctx.db.get(session.machineId);
  return (
    machine === null ||
    session.interruptedAt === undefined ||
    machine.lastHeartbeat <= session.interruptedAt
  );
}

const OPEN_STATUSES = ["pending", "active", "interrupted"] as const;

/** Whether the rider has a session, other than `except`, that may hold them, on any machine. */
async function riderHeld(
  ctx: QueryCtx | MutationCtx,
  riderId: Id<"users">,
  now: number,
  except?: Id<"sessions">,
): Promise<boolean> {
  for (const status of OPEN_STATUSES) {
    const sessions = await ctx.db
      .query("sessions")
      .withIndex("by_user_and_status", (q) =>
        q.eq("userId", riderId).eq("status", status),
      )
      .collect();
    for (const session of sessions) {
      if (session._id !== except && (await mayHold(ctx, session, now)))
        return true;
    }
  }
  return false;
}

/**
 * Whether the machine has a session that may hold it (`mayHold`). A pending
 * session of the retired recording mode holds its machine too, as any pending
 * session always did, until it is cancelled or the migration of ANH-135
 * fails it.
 */
async function machineHeld(
  ctx: QueryCtx | MutationCtx,
  machineId: Id<"machines">,
  now: number,
): Promise<boolean> {
  for (const status of OPEN_STATUSES) {
    const sessions = await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", machineId).eq("status", status),
      )
      .collect();
    for (const session of sessions) {
      const legacyWaiting =
        session.kind === undefined && session.status === "pending";
      if (legacyWaiting || (await mayHold(ctx, session, now))) return true;
    }
  }
  return false;
}

/**
 * The rider of a launch that could still be made now, or null. Whoever made
 * it could make it now, in the machine's organisation: by the role they hold
 * there, or as the Anheart admin (`rolesHeldNow`), under the rule of
 * `launchAutoSession` (`launcherRefusal`). And the rider still has an
 * account, and is still an active member of that organisation.
 */
async function riderStillAllowed(
  ctx: QueryCtx | MutationCtx,
  session: Doc<"sessions">,
  machine: Doc<"machines">,
): Promise<Doc<"users"> | null> {
  const { startedById, userId } = session;
  const organizationId = machine.organizationId;
  if (startedById === undefined || userId === undefined) return null;
  if (organizationId === undefined) return null;
  const rider = await ctx.db.get(userId);
  if (!rider || rider.clerkDeletedAt !== undefined) return null;
  if ((await organizationRoleOf(ctx, userId, organizationId)) === null)
    return null;
  for (const launcher of await rolesHeldNow(ctx, startedById, organizationId)) {
    if ((await launcherRefusal(ctx, launcher, userId, machine._id)) === null)
      return rider;
  }
  return null;
}

/**
 * Why a pending launch must not be armed now, or null when it may be. Checked
 * when the machine polls (an offer it would refuse is not made), when it
 * confirms the start (`markTrainingStarted`), and every minute for every
 * pending launch (`sweepPendingSessions`). In order:
 *
 * 1. the launch has waited no longer than `PENDING_SESSION_TTL_S`;
 * 2. the machine exists and is not deleted;
 * 3. it is still in the session's organisation;
 * 4. whoever made the launch could still make it, and the rider is still a
 *    member of that organisation (`riderStillAllowed`);
 * 5. (ANH-143, to come) the rider's aptitude and consent are valid: checked
 *    here, with a code of its own, once that ticket stores them;
 * 6. the rider's age is known and at least `MIN_RIDER_AGE`;
 * 7. the rider's max heart rate is known and is the one the launch carried,
 *    which the machine fits the programme to: a changed value is refused,
 *    not re-checked, so the machine never runs on the old one;
 * 8. the rider is in no other session that may still hold them (`mayHold`).
 */
async function armingRefusal(
  ctx: QueryCtx | MutationCtx,
  session: Doc<"sessions">,
  now: number,
): Promise<ArmingRefusal | null> {
  if (expired(session, now)) return refusalFor("expired_before_start");
  const machine = await ctx.db.get(session.machineId);
  if (!machine || machine.isDeleted)
    return refusalFor("machine_deleted_before_start");
  if (machine.organizationId !== session.organizationId)
    return refusalFor("organization_changed_before_start");
  const rider = await riderStillAllowed(ctx, session, machine);
  if (rider === null) return refusalFor("right_revoked_before_start");
  // ANH-143: aptitude and consent are checked here.
  const age = ageFrom(rider.birthYear, now);
  if (age === null || age < MIN_RIDER_AGE)
    return refusalFor("age_refused_before_start");
  const hrMax = effectiveHrMax(rider, now);
  if (hrMax === null || hrMax !== session.subjectHrMax)
    return refusalFor("hr_max_changed_before_start");
  if (await riderHeld(ctx, rider._id, now, session._id))
    return refusalFor("rider_busy");
  return null;
}

/**
 * A launch the machine must not arm: failed, with its code, before it
 * started. A failed session the gestionnaires hear of (ANH-90): the alert is
 * only scheduled here (`scheduleAlert`), so that nothing it does can fail the
 * sweep, the start route or the launch that called this. It carries the
 * session's identifier and nothing of the rider; the page of the alerts
 * words the session's `endCode` for whoever may read the session.
 */
async function failBeforeStart(
  ctx: MutationCtx,
  session: Doc<"sessions">,
  refused: ArmingRefusal,
  now: number,
): Promise<void> {
  await ctx.db.patch(session._id, {
    status: "failed",
    endedAt: now,
    endReason: refused.message,
    endCode: refused.code,
  });
  await scheduleAlert(ctx, session.machineId, "session_failed", {
    sessionId: session._id,
  });
}

/**
 * Fail every launch from the dashboard that is still pending and that its
 * machine must no longer arm (`armingRefusal`): one that waited longer than
 * `PENDING_SESSION_TTL_S` (`endReason` "expired before the machine armed
 * it"), and one whose rights, rider or machine changed while it waited. Run
 * every minute (`convex/crons.ts`). Returns how many it failed. Sessions of
 * the retired recording mode (no `kind`) are left as they are.
 */
export const sweepPendingSessions = internalMutation({
  args: {},
  returns: v.object({ failed: v.number() }),
  handler: async (ctx) => {
    const now = Date.now();
    const pending = await ctx.db
      .query("sessions")
      .withIndex("by_status_and_organization", (q) => q.eq("status", "pending"))
      .collect();
    let failed = 0;
    for (const session of pending) {
      if (session.kind !== "auto") continue;
      const refused = await armingRefusal(ctx, session, now);
      if (refused === null) continue;
      await failBeforeStart(ctx, session, refused, now);
      failed++;
    }
    return { failed };
  },
});

/**
 * Fail, as the sweep would, the launches of this rider and of this machine
 * that have waited too long: they hold neither, and would otherwise wait
 * beside the new one until the sweep's next run.
 */
async function expireWaitingLaunches(
  ctx: MutationCtx,
  riderId: Id<"users">,
  machineId: Id<"machines">,
  now: number,
): Promise<void> {
  const waiting = [
    ...(await ctx.db
      .query("sessions")
      .withIndex("by_user_and_status", (q) =>
        q.eq("userId", riderId).eq("status", "pending"),
      )
      .collect()),
    ...(await ctx.db
      .query("sessions")
      .withIndex("by_machine_and_status", (q) =>
        q.eq("machineId", machineId).eq("status", "pending"),
      )
      .collect()),
  ];
  for (const session of waiting) {
    if (session.kind === "auto" && expired(session, now)) {
      await failBeforeStart(
        ctx,
        session,
        refusalFor("expired_before_start"),
        now,
      );
    }
  }
}

/**
 * Queue an AUTO session for the machine to pick up. The only remote launch.
 *
 * A user launches for themselves only; an admin or a gestionnaire launches
 * for a rider they manage (or for themselves).
 *
 * One session at a time (ANH-144): refused with the stable code `rider_busy`
 * when the rider has a session that may still hold them on any machine, of
 * any organisation, and `machine_busy` when the machine has one (`mayHold`):
 * waiting for its machine, running, or interrupted on a machine silent since.
 * Neither refusal says where. A launch that has waited too long holds nobody:
 * it is failed on the way, as the sweep would have failed it.
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
    await limitUser(ctx, "launchAutoSession");
    await limitUser(ctx);
    const me = await getCurrentUserOrThrow(ctx);
    const riderId = args.userId ?? me._id;

    const unauthorized = await launcherRefusal(
      ctx,
      me,
      riderId,
      args.machineId,
    );
    if (unauthorized !== null) throw refusal(unauthorized);

    const machine = await ctx.db.get(args.machineId);
    if (!machine || machine.isDeleted)
      throw refusal("machine_not_found");
    // The rider is an active member of the machine's organisation.
    if (
      (await organizationRoleOf(ctx, riderId, machine.organizationId)) === null
    ) {
      throw refusal("rider_not_allowed");
    }
    if (machine.status === "offline") throw refusal("machine_offline");
    if (!machine.programsEnabled) {
      throw refusal("programs_disabled");
    }
    const now = Date.now();
    await expireWaitingLaunches(ctx, riderId, args.machineId, now);
    if (
      machine.status === "in_session" ||
      (await machineHeld(ctx, args.machineId, now))
    ) {
      throw refusal("machine_busy");
    }
    if (await riderHeld(ctx, riderId, now)) {
      throw refusal("rider_busy");
    }

    const profile = await ctx.db
      .query("machine_profiles")
      .withIndex("by_machine_and_profile", (q) =>
        q.eq("machineId", args.machineId).eq("profileId", args.profileId),
      )
      .unique();
    if (!profile) throw refusal("programme_not_on_machine");

    const rider = await ctx.db.get(riderId);
    if (!rider) throw refusal("user_not_found");
    // Both helpers give null for a value on record that is not a finite whole
    // number: what is compared below, and copied to the session, always is one.
    const hrMax = effectiveHrMax(rider, now);
    if (hrMax === null) {
      throw refusal("rider_hr_max_missing");
    }
    const unsafe = zoneRefusal(profile, hrMax);
    if (unsafe) throw refusal(unsafe);
    const age = ageFrom(rider.birthYear, now);
    if (age === null) {
      throw refusal("rider_birth_year_missing");
    }
    if (age < MIN_RIDER_AGE) {
      throw refusal("rider_too_young");
    }

    if (args.totalDurationS !== undefined) {
      if (!Number.isFinite(args.totalDurationS) || args.totalDurationS <= 0) {
        throw refusal("invalid_duration");
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
 * turning until then. Interrupted: the same request, which the machine reads
 * if it comes back with the session still running.
 */
export const requestStop = mutation({
  args: { sessionId: v.id("sessions") },
  returns: v.null(),
  handler: async (ctx, args) => {
    // Never rate limited (ANH-166): refusing a stop is never the safe side.
    const me = await getCurrentUserOrThrow(ctx);
    // A session of another organisation is a session that does not exist.
    const session = await ctx.db.get(args.sessionId);
    if (
      !session ||
      (session.userId !== me._id && !inScope(me, session.organizationId))
    )
      throw refusal("session_not_found");
    if (
      session.userId !== me._id &&
      !(await canAccessSession(ctx, session, me))
    ) {
      throw refusal("session_not_managed");
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
    if (!startedNotEnded(session.status)) return null;
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
      // The stable code of a launch refused before its machine armed it
      // (`ARMING_REFUSALS`): the site words it. Absent otherwise.
      endCode: v.optional(v.string()),
      // When the server marked it interrupted (end unknown): only while it is.
      interruptedAt: v.optional(v.number()),
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
      endCode: s.endCode,
      interruptedAt: s.interruptedAt,
      canStop: s.status === "pending" || startedNotEnded(s.status),
    };
  },
});

// ---------------------------------------------------------------------------
// Internal: what the Pi's HTTP routes call
// ---------------------------------------------------------------------------

/**
 * The session a machine names in its live state, when it is one of its own
 * sessions; undefined otherwise.
 */
async function ownSession(
  ctx: MutationCtx,
  machineId: Id<"machines">,
  named: string | undefined,
): Promise<Id<"sessions"> | undefined> {
  const sessionId =
    named === undefined ? null : ctx.db.normalizeId("sessions", named);
  if (sessionId === null) return undefined;
  const session = await ctx.db.get(sessionId);
  return session?.machineId === machineId ? sessionId : undefined;
}

/**
 * The live state a heartbeat carries. A drive that is in fault in this state
 * and was not in the previous one is an alert (`drive_fault`, ANH-90): the
 * console of this repository sends one every 10 s, in a session or not.
 */
export const updateLive = internalMutation({
  args: {
    machineId: v.id("machines"),
    live: liveStateValidator,
    programsEnabled: v.optional(v.boolean()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const machine = await ctx.db.get(args.machineId);
    if (!machine) throw machineError("machine_not_found", "Machine not found");
    const patch: Partial<Doc<"machines">> = { live: args.live };
    if (args.programsEnabled !== undefined)
      patch.programsEnabled = args.programsEnabled;
    await ctx.db.patch(args.machineId, patch);
    if (
      args.live.driveState === "fault" &&
      machine.live?.driveState !== "fault"
    ) {
      await scheduleAlert(ctx, machine._id, "drive_fault", {
        sessionId: await ownSession(ctx, machine._id, args.live.sessionId),
      });
    }
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
 *
 * Per rider, what the console needs to vet a programmed session the way
 * `launchAutoSession` does, and nothing more:
 *
 * - `userId`: the pseudonym the console writes as `subject_id` in the
 *   session record and sends back with a local session;
 * - `name`: shown on the console so the operator picks the right person.
 *   The console keeps it in memory only: never in a record, never in a log;
 * - `hrMax`: the one `launchAutoSession` would use (measured, else estimated
 *   from the birth year), null when neither is usable;
 * - `birthYear`: a whole number, or null. The console computes the age from
 *   it the way `ageFrom` does.
 *
 * Both values are set on the site by a manager (`setUserPhysiology`); the
 * console shows them and cannot change them. Fitness and consent (ANH-143)
 * are not served yet.
 */
export const getRoster = internalQuery({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      userId: v.id("users"),
      name: v.string(),
      hrMax: v.union(v.number(), v.null()),
      birthYear: v.union(v.number(), v.null()),
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
        birthYear: isWholeNumber(user.birthYear) ? user.birthYear : null,
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
    // Never a launch the machine would be refused when it arms it (expired,
    // a right withdrawn, the rider's physiology changed...): the sweep fails
    // it within the minute. Nor one that expires within `POLL_MARGIN_S`.
    const now = Date.now();
    for (const s of pending) {
      if (
        s.kind !== "auto" ||
        expiresSoon(s, now) ||
        s.profileId === undefined ||
        s.subjectHrMax === undefined ||
        s.userId === undefined ||
        (await armingRefusal(ctx, s, now)) !== null
      ) {
        continue;
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
    }
    return null;
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
 *
 * The launch is checked again first (`armingRefusal`). Refused, it becomes a
 * failed session with the refusal's code, the machine stays as it was, and
 * `{armed: false, code, message}` is returned, which the start route answers
 * 409: the machine must not run. A launch already failed that way gets the
 * same answer again, so that a confirmation sent twice reads the same.
 */
export const markTrainingStarted = internalMutation({
  args: {
    machineId: v.id("machines"),
    sessionId: v.id("sessions"),
    machineStartedAt: v.optional(v.number()),
    sessionAgeMs: v.optional(v.number()),
  },
  returns: v.union(
    v.object({ armed: v.literal(true) }),
    v.object({
      armed: v.literal(false),
      code: armingRefusalCode,
      message: v.string(),
    }),
  ),
  handler: async (ctx, args) => {
    const s = await ctx.db.get(args.sessionId);
    if (!s || s.machineId !== args.machineId)
      throw machineError("session_not_found", "Session not found");
    if (s.status === "failed" && isArmingRefusalCode(s.endCode)) {
      return { armed: false as const, ...refusalFor(s.endCode) };
    }
    if (s.status !== "pending")
      throw machineError(
        "session_not_pending",
        `Session is not pending (status: ${s.status})`,
      );
    const now = Date.now();
    const refused = await armingRefusal(ctx, s, now);
    if (refused !== null) {
      await failBeforeStart(ctx, s, refused, now);
      return { armed: false as const, ...refused };
    }
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
    return { armed: true as const };
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
    const userId =
      claimedRider &&
      (await ctx.db.get(claimedRider)) !== null &&
      (await organizationRoleOf(ctx, claimedRider, machine.organizationId)) !==
        null &&
      (await hasLaunchRight(ctx, args.machineId, claimedRider))
        ? claimedRider
        : null;
    const unassigned = args.userId !== undefined && userId === null;
    const notes = [
      ...(args.occupancy ? [`Occupancy: ${args.occupancy}`] : []),
      ...(unassigned
        ? ["Passenger attribution unavailable at registration"]
        : []),
    ];
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
      notes: notes.length > 0 ? notes.join("; ") : undefined,
      kind: args.kind,
      origin: "local",
      localRef: args.localRef,
      profileId: args.profileId,
      profileName: args.profileName,
      zoneLowBpm: args.zoneLowBpm,
      zoneHighBpm: args.zoneHighBpm,
      totalDurationS: args.totalDurationS,
      subjectHrMax: args.subjectHrMax,
      subjectLabel: unassigned ? undefined : args.subjectLabel,
      operatorName: args.operatorName,
    });
    await ctx.db.patch(args.machineId, { status: "in_session" });
    return id;
  },
});

/**
 * The machine ends a session of its own. An `interrupted` one too: the end the
 * server had no news of arrives, and closes it as it would have then.
 */
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
      interruptedAt: undefined,
    });
    await ctx.db.patch(args.machineId, { status: "online" });
    // The provisional summary of the session, from its telemetry (ANH-89):
    // the machine sends the end once its points are acknowledged. The
    // deposited record replaces it (`sessionReports.summarizeRecord`).
    await ctx.scheduler.runAfter(
      0,
      internal.sessionReports.summarizeTelemetry,
      { sessionId: args.sessionId },
    );
    // A failed session is an alert (ANH-90); its reason stays on the session.
    if (args.failed) {
      await scheduleAlert(ctx, args.machineId, "session_failed", {
        sessionId: s._id,
      });
    }
    return null;
  },
});

/**
 * What the machine is told of a session it asks about. `active` is whether the
 * server holds it started and not ended: true for an `interrupted` session as
 * well. The machine stops its session on `active: false`, and the server never
 * asks a running session to stop because it had no news of it.
 */
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
      active: startedNotEnded(s.status),
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
    // What the events stored by this call alert of (ANH-90): an event sent
    // again alerts of nothing.
    const verdicts = { count: 0, detail: undefined as string | undefined };
    const faults = { count: 0, detail: undefined as string | undefined };
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
      if (event.kind === "verdict" && verdictAlerts(event.detail)) {
        verdicts.count++;
        verdicts.detail = foldedDetail(
          "safety_verdict",
          verdicts.detail,
          verdictAction(event.detail),
        );
      } else if (event.kind === "drive_fault" && driveFaultAlerts(event.detail)) {
        faults.count++;
        faults.detail = foldedDetail(
          "drive_fault",
          faults.detail,
          driveFaultCode(event.detail),
        );
      }
    }
    if (verdicts.count > 0) {
      await scheduleAlert(ctx, args.machineId, "safety_verdict", {
        sessionId: s._id,
        ...verdicts,
      });
    }
    if (faults.count > 0) {
      await scheduleAlert(ctx, args.machineId, "drive_fault", {
        sessionId: s._id,
        ...faults,
      });
    }
    return outcome;
  },
});

// ---------------------------------------------------------------------------
// A session the machine no longer gives news of
// ---------------------------------------------------------------------------

/**
 * The first news of a session, on the server's clock: the creation of its row
 * by the machine's declaration (a session started at the machine), or the
 * confirmation of its start (a launch from the dashboard, whose `startedAt`
 * is then a date of the server, never before the launch).
 */
function firstNewsOf(session: Doc<"sessions">): number {
  return session.origin === "remote"
    ? Math.max(session._creationTime, session.startedAt)
    : session._creationTime;
}

/**
 * Whether the server has heard of `session` after `since`, on its own clock:
 * its first news, the reception of its point of greatest `t` or of its event
 * of greatest rank, or a heartbeat of its machine naming it as the session the
 * machine runs. No date the machine wrote is read.
 */
async function heardOfSince(
  ctx: QueryCtx,
  session: Doc<"sessions">,
  since: number,
): Promise<boolean> {
  if (firstNewsOf(session) > since) return true;
  const point = await ctx.db
    .query("training_telemetry")
    .withIndex("by_session_and_t", (q) => q.eq("sessionId", session._id))
    .order("desc")
    .first();
  if (point !== null && point._creationTime > since) return true;
  const event = await ctx.db
    .query("training_events")
    .withIndex("by_session_and_seq", (q) => q.eq("sessionId", session._id))
    .order("desc")
    .first();
  if (event !== null && event._creationTime > since) return true;
  const beats = ctx.db
    .query("machine_heartbeats")
    .withIndex("by_machine_and_timestamp", (q) =>
      q.eq("machineId", session.machineId).gt("timestamp", since),
    );
  for await (const beat of beats) {
    if (beat.activeSessionId === session._id) return true;
  }
  return false;
}

/**
 * Mark `interrupted` every active training session the server has heard
 * nothing of for `SESSION_SILENCE_MS` (`heardOfSince`): its console stopped
 * before sending its end, its machine is off, or its link has been down that
 * long. Run once a minute by the job of `machines.checkOfflineMachines`.
 * Returns how many it marked.
 *
 * Never `completed` nor `failed`: its end is unknown, and it may still be
 * running on a machine that cannot reach the server. Nothing is asked of the
 * machine: `getTrainingStatus` still answers that the session is active, a
 * stop asked for on the site still reaches it, the next heartbeat naming it
 * makes it active again (`resumeNamedSession`), and the end the machine sends
 * closes it. Sessions of the retired recording mode (no `kind`) are history,
 * left as they are.
 */
export async function interruptSilentSessions(
  ctx: MutationCtx,
  now: number,
): Promise<number> {
  const since = now - SESSION_SILENCE_MS;
  const active = await ctx.db
    .query("sessions")
    .withIndex("by_status_and_organization", (q) => q.eq("status", "active"))
    .collect();
  let marked = 0;
  for (const session of active) {
    if (session.kind === undefined) continue;
    if (await heardOfSince(ctx, session, since)) continue;
    await ctx.db.patch(session._id, {
      status: "interrupted",
      interruptedAt: now,
    });
    marked++;
  }
  return marked;
}

/**
 * A heartbeat of `machineId` names `sessionId` as the session it runs: a
 * session the server had marked `interrupted` is active again. A machine only
 * ever revives a session of its own; any other session is left as it is.
 */
export async function resumeNamedSession(
  ctx: MutationCtx,
  machineId: Id<"machines">,
  sessionId: Id<"sessions">,
): Promise<void> {
  const session = await ctx.db.get(sessionId);
  if (
    session === null ||
    session.machineId !== machineId ||
    session.status !== "interrupted"
  ) {
    return;
  }
  await ctx.db.patch(sessionId, { status: "active", interruptedAt: undefined });
}
