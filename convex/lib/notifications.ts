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

export type { NotificationType };

/**
 * Each kind of alert: its priority within a session, its fallback grouping,
 * and whether it goes by e-mail too for an account that has not chosen
 * (`notification_settings`).
 *
 * - "machine": a state of the machine. A new event folds into the
 *   recipient's unread alert of that kind for that machine.
 * - "session": something that happened in one session. It folds into the
 *   recipient's unread alert for that session only. With a session, all
 *   kinds with a positive session priority fold together, keeping the highest.
 */
export const NOTIFICATION_TYPES = {
  machine_offline: {
    foldsBy: "machine",
    sessionPriority: 0,
    emailByDefault: false,
  },
  session_failed: {
    foldsBy: "session",
    sessionPriority: 1,
    emailByDefault: true,
  },
  drive_fault: {
    foldsBy: "machine",
    sessionPriority: 3,
    emailByDefault: true,
  },
  safety_verdict: {
    foldsBy: "session",
    sessionPriority: 2,
    emailByDefault: true,
  },
  // To the Anheart admins: never folded into an alert of the machine's staff.
  incident_high: {
    foldsBy: "session",
    sessionPriority: 0,
    emailByDefault: false,
  },
} as const satisfies Record<
  NotificationType,
  {
    foldsBy: "machine" | "session";
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
 * How many unread alerts one lookup of a delivery reads. Folding keeps one
 * unread alert per session and per kind without one, so only the latest
 * alerts of a machine read by a fault without a session can reach it.
 */
const FOLD_SCAN = 50;

/** How many of a session's last events are read for a drive fault, which ends it. */
const FAULT_SCAN = 50;

/**
 * Whether the session's record holds a drive fault: its `drive_fault` event,
 * or the verdict of the Pi's `drive_fault` rule, whichever is stored first.
 */
async function sessionFaulted(
  ctx: QueryCtx,
  sessionId: Id<"sessions">,
): Promise<boolean> {
  const events = await ctx.db
    .query("training_events")
    .withIndex("by_session_and_seq", (q) => q.eq("sessionId", sessionId))
    .order("desc")
    .take(FAULT_SCAN);
  return events.some((row) =>
    row.kind === "drive_fault"
      ? driveFaultAlerts(row.detail)
      : row.kind === "verdict" && row.detail.startsWith("drive_fault ("),
  );
}

/**
 * EX-5 (ANH-237): a drive fault whose heartbeat named no session, and a
 * session whose record holds a drive fault, are one event, whichever is
 * delivered first. For an event of a session: the machine's unread fault
 * alert without a session. For a fault without a session: the unread alert
 * of a session changed last. Undefined unless that session faulted.
 */
async function sameFault(
  ctx: QueryCtx,
  unread: Doc<"notifications">[],
  type: NotificationType,
  event: MachineEvent,
): Promise<Doc<"notifications"> | undefined> {
  if (NOTIFICATION_TYPES[type].sessionPriority === 0) return undefined;
  let other: Doc<"notifications"> | undefined;
  if (event.sessionId !== undefined) {
    other = unread.find(
      (row) => row.sessionId === undefined && row.type === "drive_fault",
    );
  } else if (type === "drive_fault") {
    other = unread
      .filter(
        (row) =>
          row.sessionId !== undefined &&
          NOTIFICATION_TYPES[row.type].sessionPriority > 0,
      )
      .sort((a, b) => b.updatedAt - a.updatedAt)[0];
  }
  const sessionId = event.sessionId ?? other?.sessionId;
  return other && sessionId && (await sessionFaulted(ctx, sessionId))
    ? other
    : undefined;
}

/**
 * Write or fold one recipient's alert inside the subtransaction of
 * `deliverRecipient`, returning one only for a new alert. Queue its optional
 * REST e-mail separately from the write so an e-mail setup failure keeps the
 * site alert and records a safe failure code on it.
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
      .take(FOLD_SCAN);
  // The session's unread alerts and those without a session; for a fault
  // without a session, also the machine's latest unread ones, newest first.
  const unread = [
    ...(event.sessionId === undefined ? [] : await unreadOf(event.sessionId)),
    ...(await unreadOf(undefined)),
    ...(event.sessionId === undefined && type === "drive_fault"
      ? await ctx.db
          .query("notifications")
          .withIndex("by_user_and_organization_and_machine_and_read", (q) =>
            q
              .eq("userId", user._id)
              .eq("organizationId", organizationId)
              .eq("machineId", machine._id)
              .eq("read", false),
          )
          .order("desc")
          .take(FOLD_SCAN)
      : []),
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
    unread.find(
      (row) =>
        row.type === type &&
        (NOTIFICATION_TYPES[type].foldsBy === "machine"
          ? event.sessionId === undefined || row.sessionId === undefined
          : row.sessionId === event.sessionId),
    ) ??
    (await sameFault(ctx, unread, type, event));
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
            ? detail
            : same.detail,
      sessionId: same.sessionId ?? event.sessionId,
    });
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
  if (emailSettings() === null || user.email.trim() === "") return 1;
  try {
    const choices = emailChoices(
      await ctx.db
        .query("notification_settings")
        .withIndex("by_user", (q) => q.eq("userId", user._id))
        .unique(),
    );
    if (!choices[type]) return 1;
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
