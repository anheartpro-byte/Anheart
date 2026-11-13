/**
 * Alerts to the gestionnaires (ANH-90): who receives one, how a new event
 * folds into an alert not read yet, and the e-mail that may go with a new one.
 *
 * Asked for by the server where it detects the event, never by a client:
 * - `machine_offline`: `machines.checkOfflineMachines`, when it marks a
 *   machine offline (no heartbeat for `LIVE_FRESH_MS`, checked every minute);
 * - `drive_fault`: `training.updateLive`, when a heartbeat says the drive is
 *   in fault and the heartbeat before did not; and `training.storeEvents`,
 *   for a `drive_fault` event of a session's record;
 * - `session_failed`: `training.endTrainingSession`, when the machine ends a
 *   session as failed, and every launch failed before its machine armed it
 *   (ANH-144: the sweep, the start route, a launch that expired);
 * - `safety_verdict`: `training.storeEvents`, for a `verdict` event whose
 *   action ends the session (`RAMP_DOWN`, `QUICK_STOP`, `GO_SILENT`, or an
 *   action this server does not know). `FREEZE` and `REDUCE`, which keep the
 *   session going, are the regulation at work: no alert.
 *
 * Each of them only schedules the alert (`scheduleAlert`), written at once by
 * a mutation of its own (`notifications.deliver`): whatever happens while it
 * is written, or the e-mail queued, never fails nor undoes the heartbeat, the
 * batch of events, the end, the start, the sweep or the offline check that
 * asked for it.
 * Each recipient is written in a separate subtransaction, so only failed
 * recipients are retried. A new alert may queue a REST e-mail action in
 * `notificationEmails`, whose delivery state and retries stay on that alert.
 *
 * - `incident_high`: `convex/lib/incidents.ts`, when a `high` incident opens
 *   (ANH-145).
 *
 * Recipients: the gestionnaires linked to the machine who are still active
 * gestionnaires of its organisation, and the active admins of that
 * organisation. Nobody else: not a patient, not the Anheart team as such
 * (its fleet alerts are ANH-150's). Except `incident_high`, which goes to the
 * active admins of the Anheart organisation only, in that organisation: they
 * are the ones who close it.
 */
import { internal } from "../_generated/api";
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import {
  anheartClerkOrganizationId,
  DEFAULT_ORGANIZATION_SLUG,
  isAnheartOrganization,
  organizationRoleOf,
} from "./auth";
import { emailSettings, type NotificationType } from "./notificationEmail";
import { LIVE_FRESH_MS } from "../../lib/training";

export type { NotificationType };

/**
 * Each kind of alert: its priority within a session, and whether it goes by
 * e-mail too for an account that has not chosen (`notification_settings`).
 *
 * With a session, every kind with a positive session priority folds into the
 * recipient's unread alert of that session, keeping the highest. Otherwise a
 * kind folds into the recipient's unread alert of the same kind and session,
 * or without a session: `machine_offline` by machine, with no time bound.
 * A drive fault only folds into the same fault, within `SAME_FAULT_MS`
 * (`sameFault`).
 */
export const NOTIFICATION_TYPES = {
  machine_offline: {
    sessionPriority: 0,
    emailByDefault: false,
  },
  session_failed: {
    sessionPriority: 1,
    emailByDefault: true,
  },
  drive_fault: {
    sessionPriority: 3,
    emailByDefault: true,
  },
  safety_verdict: {
    sessionPriority: 2,
    emailByDefault: true,
  },
  // To the Anheart admins: never folded into an alert of the machine's staff.
  incident_high: {
    sessionPriority: 0,
    emailByDefault: false,
  },
} as const satisfies Record<
  NotificationType,
  {
    sessionPriority: number;
    emailByDefault: boolean;
  }
>;

export const NOTIFICATION_TYPE_NAMES = Object.keys(
  NOTIFICATION_TYPES,
) as NotificationType[];

/** What an account receives by e-mail, its choices completed by the defaults. */
export function emailChoices(
  settings: Pick<Doc<"notification_settings">, "email"> | null,
): Record<NotificationType, boolean> {
  return Object.fromEntries(
    NOTIFICATION_TYPE_NAMES.map((type) => [
      type,
      settings?.email[type] ?? NOTIFICATION_TYPES[type].emailByDefault,
    ]),
  ) as Record<NotificationType, boolean>;
}

// ---------------------------------------------------------------------------
// What the machine said, as a code
// ---------------------------------------------------------------------------

/** A code the machine sent, kept as it is; anything else is not kept. */
const CODE = /^[A-Z][A-Z0-9_]{0,31}$/;

/**
 * The safety actions that end a session, least severe first: those of the
 * Pi's `SafetyAction` (raspberry-pi/src/training/types.py) from `RAMP_DOWN`.
 */
export const STOPPING_ACTIONS = ["RAMP_DOWN", "QUICK_STOP", "GO_SILENT"];
/** The actions that keep the session going: the regulation, not an alert. */
const REGULATING_ACTIONS = ["FREEZE", "REDUCE"];

/**
 * The action a `verdict` event of a session's record demanded, as the Pi
 * writes it (`raspberry-pi/src/record/session.py`): "<rule> (<ACTION>):
 * <detail>", or "cleared" when the verdict is lifted. Null when the text is
 * not of that form.
 */
export function verdictAction(detail: string): string | null {
  const action = /^[a-z0-9_]+ \(([A-Z_]+)\)/.exec(detail)?.[1];
  return action !== undefined && CODE.test(action) ? action : null;
}

/**
 * Whether a `verdict` event calls for an alert: not when it lifts the
 * verdict, nor when its action keeps the session going. An action this
 * server cannot read does: the safe side is to warn.
 */
export function verdictAlerts(detail: string): boolean {
  if (detail.trim() === "cleared") return false;
  const action = verdictAction(detail);
  return action === null || !REGULATING_ACTIONS.includes(action);
}

/** Whether a `drive_fault` event calls for an alert: not when it lifts the fault. */
export function driveFaultAlerts(detail: string): boolean {
  return detail.trim() !== "cleared";
}

/**
 * The drive's fault mnemonic of a `drive_fault` event, as the Pi writes it:
 * "<MNEMONIC> (LFT <code>): <message>". Null when the text is not of that
 * form, "cleared" included.
 */
export function driveFaultCode(detail: string): string | null {
  const code = /^([A-Z0-9_]+) \(LFT\b/.exec(detail)?.[1];
  return code !== undefined && CODE.test(code) ? code : null;
}

/** The more severe of two verdict actions; an unknown one counts as the most. */
function severity(action: string | undefined): number {
  if (action === undefined) return -1;
  const rank = STOPPING_ACTIONS.indexOf(action);
  return rank === -1 ? STOPPING_ACTIONS.length : rank;
}

/**
 * The code an alert keeps when a new event folds into it: for a verdict the
 * most severe action, for anything else the latest code the machine sent.
 */
export function foldedDetail(
  type: NotificationType,
  kept: string | undefined,
  next: string | null | undefined,
): string | undefined {
  const sent = next ?? undefined;
  if (type === "safety_verdict") {
    return severity(sent) > severity(kept) ? sent : kept;
  }
  return sent ?? kept;
}

// ---------------------------------------------------------------------------
// Who receives an alert, and writing it
// ---------------------------------------------------------------------------

/** Recheck the active organisation role and, for a gestionnaire, its machine link. */
export async function isMachineRecipient(
  ctx: QueryCtx,
  machineId: Id<"machines">,
  organizationId: Id<"organizations">,
  userId: Id<"users">,
): Promise<boolean> {
  const role = await organizationRoleOf(ctx, userId, organizationId);
  if (role === "admin") return true;
  if (role !== "gestionnaire") return false;
  const links = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_machine_and_gestionnaire", (q) =>
      q.eq("machineId", machineId).eq("gestionnaireId", userId),
    )
    .collect();
  return links.some((link) => link.organizationId === organizationId);
}

/**
 * The accounts an alert about this machine goes to: the active admins of its
 * organisation, and the gestionnaires linked to it who are still active
 * gestionnaires there. An account deleted in Clerk receives nothing.
 */
export async function recipientsOf(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  organizationId: Id<"organizations">,
): Promise<Doc<"users">[]> {
  const ids = new Set<Id<"users">>();
  const members = await ctx.db
    .query("memberships")
    .withIndex("by_active_and_organization", (q) =>
      q.eq("active", true).eq("organizationId", organizationId),
    )
    .collect();
  for (const member of members) {
    if (member.role === "admin") ids.add(member.userId);
  }
  const links = await ctx.db
    .query("machine_gestionnaires")
    .withIndex("by_machine", (q) => q.eq("machineId", machine._id))
    .collect();
  for (const link of links) {
    ids.add(link.gestionnaireId);
  }
  const users: Doc<"users">[] = [];
  for (const id of ids) {
    if (!(await isMachineRecipient(ctx, machine._id, organizationId, id)))
      continue;
    const user = await ctx.db.get(id);
    if (user && user.clerkDeletedAt === undefined) users.push(user);
  }
  return users;
}

/** The Anheart organisation (`isAnheartOrganization`), or null when it has none. */
export async function anheartOrganization(
  ctx: QueryCtx,
): Promise<Doc<"organizations"> | null> {
  const clerkOrgId = anheartClerkOrganizationId();
  const candidates =
    clerkOrgId === null
      ? await ctx.db
          .query("organizations")
          .withIndex("by_slug", (q) => q.eq("slug", DEFAULT_ORGANIZATION_SLUG))
          .collect()
      : await ctx.db
          .query("organizations")
          .withIndex("by_clerk_org_id", (q) => q.eq("clerkOrgId", clerkOrgId))
          .collect();
  return (
    candidates.find(
      (row) => row.deletedAt === undefined && isAnheartOrganization(row),
    ) ?? null
  );
}

/** The active admins of the Anheart organisation: who hears of a high incident. */
async function anheartAdmins(ctx: MutationCtx): Promise<Doc<"users">[]> {
  const organization = await anheartOrganization(ctx);
  if (organization === null) return [];
  const members = await ctx.db
    .query("memberships")
    .withIndex("by_active_and_organization", (q) =>
      q.eq("active", true).eq("organizationId", organization._id),
    )
    .collect();
  const users: Doc<"users">[] = [];
  for (const member of members) {
    if (member.role !== "admin") continue;
    const user = await ctx.db.get(member.userId);
    if (user && user.clerkDeletedAt === undefined) users.push(user);
  }
  return users;
}

export type MachineEvent = {
  /** The session it happened in, for the kinds that fold by session. */
  sessionId?: Id<"sessions">;
  /** A code the machine sent (`verdictAction`, `driveFaultCode`). */
  detail?: string | null;
  /** How many events of this kind this call reports, 1 by default. */
  count?: number;
};

/** Bound failed alert writes to three delivery attempts before logging a final code. */
export const DELIVERY_MAX_ATTEMPTS = 3;
/** Space the two possible delivery retries to let a transient failure recover. */
export const DELIVERY_RETRY_DELAYS_MS = [1_000, 10_000] as const;

/** Carry the attempt and only failed recipients, or omit recipients to retry discovery. */
export type DeliveryRetry = { recipientIds?: Id<"users">[]; attempt?: number };

/**
 * Reschedule failed delivery within the attempt bound, preserving its recipients.
 * After the last attempt, log one final line of codes and IDs without error text.
 */
export async function retryDelivery(
  ctx: MutationCtx,
  machineId: Id<"machines">,
  type: NotificationType,
  event: MachineEvent,
  retry: DeliveryRetry,
): Promise<void> {
  const attempt = retry.attempt ?? 1;
  if (attempt >= DELIVERY_MAX_ATTEMPTS) {
    console.error({
      code: "notification_delivery_failed",
      machineId,
      type,
      sessionId: event.sessionId,
      recipientIds: retry.recipientIds,
      attempts: attempt,
    });
    return;
  }
  await ctx.scheduler.runAfter(
    DELIVERY_RETRY_DELAYS_MS[attempt - 1],
    internal.notifications.deliver,
    {
      machineId,
      type,
      sessionId: event.sessionId,
      detail: event.detail ?? undefined,
      count: event.count ?? 1,
      recipientIds: retry.recipientIds,
      attempt: attempt + 1,
    },
  );
}

/**
 * How many unread alerts one lookup of a delivery reads. Folding leaves one
 * unread alert per session, and the time bound below leaves few without a
 * session; past this many, a delivery writes one alert more, never fewer.
 */
const FOLD_SCAN = 50;

/** How many of a session's last events are read for a drive fault, which ends it. */
const FAULT_SCAN = 50;

/**
 * How far apart a fault without a session and a session's fault may be heard
 * and still be one fault: the console sends a heartbeat every 10 s, and past
 * `LIVE_FRESH_MS` without one the server holds the machine offline and can no
 * longer tell one fault from the next. Further apart, a new alert.
 */
export const SAME_FAULT_MS = LIVE_FRESH_MS;

/**
 * Whether the session's record holds a drive fault received since `since`
 * (server clock): its `drive_fault` event, or the verdict of the Pi's
 * `drive_fault` rule, whichever is stored first.
 */
async function sessionFaulted(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
  since: number,
): Promise<boolean> {
  const events = await ctx.db
    .query("training_events")
    .withIndex("by_session_and_seq", (q) => q.eq("sessionId", sessionId))
    .order("desc")
    .take(FAULT_SCAN);
  return events.some(
    (row) =>
      row._creationTime >= since &&
      (row.kind === "drive_fault"
        ? driveFaultAlerts(row.detail)
        : row.kind === "verdict" && row.detail.startsWith("drive_fault (")),
  );
}

/**
 * EX-5 (ANH-237): the unread alert a drive fault shares with this event, the
 * same fault inside the time bound. A fault whose heartbeat named no session
 * folds into the machine's fault alert without a session changed within
 * `SAME_FAULT_MS`, else into the session alert changed last whose record
 * received a drive fault within `SAME_FAULT_MS`. An event of a session takes
 * the machine's fault alert without a session written while that session ran
 * (from its start to `SAME_FAULT_MS` after its end), when it is a fault or
 * the session's record holds one. Undefined otherwise: a new alert.
 */
async function sameFault(
  ctx: QueryCtx,
  unread: Doc<"notifications">[],
  recent: () => Promise<Doc<"notifications">[]>,
  type: NotificationType,
  event: MachineEvent,
  now: number,
): Promise<Doc<"notifications"> | undefined> {
  const loose = unread.filter(
    (row) => row.sessionId === undefined && row.type === "drive_fault",
  );
  if (event.sessionId === undefined) {
    if (type !== "drive_fault") return undefined;
    const fresh = loose.find((row) => now - row.updatedAt <= SAME_FAULT_MS);
    if (fresh) return fresh;
    const latest = (await recent())
      .filter(
        (row) =>
          !row.read &&
          row.sessionId !== undefined &&
          NOTIFICATION_TYPES[row.type].sessionPriority > 0,
      )
      .sort((a, b) => b.updatedAt - a.updatedAt)[0];
    return latest?.sessionId !== undefined &&
      (await sessionFaulted(ctx, latest.sessionId, now - SAME_FAULT_MS))
      ? latest
      : undefined;
  }
  if (NOTIFICATION_TYPES[type].sessionPriority === 0) return undefined;
  const session = await ctx.db.get(event.sessionId);
  const during = loose.find(
    (row) =>
      session !== null &&
      row.createdAt >= session.startedAt &&
      (session.endedAt === undefined ||
        row.createdAt <= session.endedAt + SAME_FAULT_MS),
  );
  return during !== undefined &&
    (type === "drive_fault" ||
      (await sessionFaulted(ctx, event.sessionId, 0)))
    ? during
    : undefined;
}

/**
 * Queue the REST e-mail of a recipient's alert of `type` when its settings
 * want one, recording a safe failure code if queuing fails: the site alert
 * stays either way.
 */
async function queueEmail(
  ctx: MutationCtx,
  notificationId: Id<"notifications">,
  user: Doc<"users">,
  type: NotificationType,
  now: number,
): Promise<void> {
  if (emailSettings() === null || user.email.trim() === "") return;
  try {
    const choices = emailChoices(
      await ctx.db
        .query("notification_settings")
        .withIndex("by_user", (q) => q.eq("userId", user._id))
        .unique(),
    );
    if (!choices[type]) return;
    await ctx.db.patch(notificationId, {
      email: {
        type,
        status: "pending",
        attempts: 0,
        queuedAt: now,
        updatedAt: now,
      },
    });
    await ctx.scheduler.runAfter(0, internal.notificationEmails.send, {
      notificationId,
      attempt: 0,
    });
  } catch {
    await ctx.db.patch(notificationId, {
      email: {
        type,
        status: "failed",
        attempts: 0,
        queuedAt: now,
        updatedAt: now,
        failureCode: "email_queue_failed",
      },
    });
  }
}

/**
 * Write or fold one recipient's alert inside the subtransaction of
 * `deliverRecipient`, returning one only for a new alert. Queue its optional
 * REST e-mail separately from the write so an e-mail setup failure keeps the
 * site alert and records a safe failure code on it. A fold that promotes an
 * alert no e-mail was queued for queues one for its new type.
 */
export async function notifyRecipient(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  user: Doc<"users">,
  type: NotificationType,
  event: MachineEvent,
): Promise<number> {
  // A high incident's alert is the Anheart admins', in their organisation.
  const organizationId =
    type === "incident_high"
      ? (await anheartOrganization(ctx))?._id
      : machine.organizationId;
  if (organizationId === undefined) return 0;
  const now = Date.now();
  const detail =
    event.detail !== undefined &&
    event.detail !== null &&
    CODE.test(event.detail)
      ? event.detail
      : undefined;
  const unreadOf = (sessionId: Id<"sessions"> | undefined) =>
    ctx.db
      .query("notifications")
      .withIndex(
        "by_user_and_organization_and_machine_and_read_and_session",
        (q) =>
          q
            .eq("userId", user._id)
            .eq("organizationId", organizationId)
            .eq("machineId", machine._id)
            .eq("read", false)
            .eq("sessionId", sessionId),
      )
      .order("desc")
      .take(FOLD_SCAN);
  // The machine's alerts changed within the time bound of a fault.
  const recent = () =>
    ctx.db
      .query("notifications")
      .withIndex("by_user_and_organization_and_machine_and_updated", (q) =>
        q
          .eq("userId", user._id)
          .eq("organizationId", organizationId)
          .eq("machineId", machine._id)
          .gte("updatedAt", now - SAME_FAULT_MS),
      )
      .take(FOLD_SCAN);
  // The session's unread alerts, then those without a session, newest first.
  const unread = [
    ...(event.sessionId === undefined ? [] : await unreadOf(event.sessionId)),
    ...(await unreadOf(undefined)),
  ];
  const sameSession =
    event.sessionId !== undefined &&
    NOTIFICATION_TYPES[type].sessionPriority > 0
      ? unread.find(
          (row) =>
            row.sessionId === event.sessionId &&
            NOTIFICATION_TYPES[row.type].sessionPriority > 0,
        )
      : undefined;
  const same =
    sameSession ??
    // A drive fault only folds into the same fault (`sameFault`).
    (type === "drive_fault"
      ? undefined
      : unread.find(
          (row) => row.type === type && row.sessionId === event.sessionId,
        )) ??
    (await sameFault(ctx, unread, recent, type, event, now));
  if (same) {
    const promoted =
      NOTIFICATION_TYPES[type].sessionPriority >
      NOTIFICATION_TYPES[same.type].sessionPriority;
    await ctx.db.patch(same._id, {
      type: promoted ? type : same.type,
      count: same.count + (event.count ?? 1),
      updatedAt: now,
      detail:
        same.type === type
          ? foldedDetail(type, same.detail, detail)
          : promoted
            ? (detail ?? same.detail)
            : same.detail,
      sessionId: same.sessionId ?? event.sessionId,
    });
    if (promoted && same.email === undefined)
      await queueEmail(ctx, same._id, user, type, now);
    return 0;
  }
  const notificationId = await ctx.db.insert("notifications", {
    organizationId,
    userId: user._id,
    type,
    machineId: machine._id,
    sessionId: event.sessionId,
    detail,
    count: event.count ?? 1,
    createdAt: now,
    updatedAt: now,
    read: false,
  });
  await queueEmail(ctx, notificationId, user, type, now);
  return 1;
}

/**
 * Alert the machine's active organisation admins and linked gestionnaires,
 * folding into their unread alert by machine or session and queuing an e-mail
 * only for a new alert when their settings allow it. Each recipient has its
 * own subtransaction, so a failed write retries only the failed recipients
 * without recounting events for those already served. Return the new alert count.
 * Do nothing for a deleted machine, one without an organisation, or a deleted
 * organisation.
 */
export async function notifyMachine(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  type: NotificationType,
  event: MachineEvent = {},
  retry: DeliveryRetry = {},
): Promise<number> {
  const organizationId = machine.organizationId;
  if (machine.isDeleted || organizationId === undefined) return 0;
  let users: Doc<"users">[];
  try {
    const organization = await ctx.db.get(organizationId);
    if (!organization || organization.deletedAt !== undefined) return 0;
    users =
      type === "incident_high"
        ? await anheartAdmins(ctx)
        : await recipientsOf(ctx, machine, organizationId);
  } catch {
    await retryDelivery(ctx, machine._id, type, event, retry);
    return 0;
  }
  let written = 0;
  const failed: Id<"users">[] = [];
  for (const user of users) {
    if (
      retry.recipientIds !== undefined &&
      !retry.recipientIds.includes(user._id)
    )
      continue;
    try {
      written += await ctx.runMutation(
        internal.notifications.deliverRecipient,
        {
          machineId: machine._id,
          userId: user._id,
          type,
          sessionId: event.sessionId,
          detail: event.detail ?? undefined,
          count: event.count ?? 1,
        },
      );
    } catch {
      failed.push(user._id);
    }
  }
  if (failed.length > 0) {
    await retryDelivery(ctx, machine._id, type, event, {
      ...retry,
      recipientIds: failed,
    });
  }
  return written;
}

/** How long an alert is kept, read or not. */
export const NOTIFICATION_RETENTION_MS = 90 * 24 * 60 * 60 * 1000;

/**
 * Ask for an alert about this machine, written at once by a mutation of its
 * own (`notifications.deliver`, which calls `notifyMachine`), not by the one
 * that saw the event: a failure there can neither fail nor undo the caller.
 * Only identifiers and a machine-side code are passed on, never a sentence.
 */
export async function scheduleAlert(
  ctx: MutationCtx,
  machineId: Id<"machines">,
  type: NotificationType,
  event: MachineEvent = {},
): Promise<void> {
  await ctx.scheduler.runAfter(0, internal.notifications.deliver, {
    machineId,
    type,
    sessionId: event.sessionId,
    detail: event.detail ?? undefined,
    count: event.count ?? 1,
  });
}
