/**
 * Alerts to the gestionnaires (ANH-90): who receives one, how a new event
 * folds into an alert not read yet, and the e-mail that may go with a new one.
 *
 * Written by the server where it detects the event, never by a client:
 * - `machine_offline`: `machines.checkOfflineMachines`, when it marks a
 *   machine offline (no heartbeat for `LIVE_FRESH_MS`, checked every minute);
 * - `drive_fault`: `training.updateLive`, when a heartbeat says the drive is
 *   in fault and the heartbeat before did not; and `training.storeEvents`,
 *   for a `drive_fault` event of a session's record;
 * - `session_failed`: `training.endTrainingSession`, when the machine ends a
 *   session as failed;
 * - `safety_verdict`: `training.storeEvents`, for a `verdict` event whose
 *   action ends the session (`RAMP_DOWN`, `QUICK_STOP`, `GO_SILENT`, or an
 *   action this server does not know). `FREEZE` and `REDUCE`, which keep the
 *   session going, are the regulation at work: no alert.
 *
 * Recipients: the gestionnaires linked to the machine who are still active
 * gestionnaires of its organisation, and the active admins of that
 * organisation. Nobody else: not a patient, not the Anheart team as such
 * (its fleet alerts are ANH-150's).
 *
 * Extension point: an incident on a machine (ANH-145) is one more type
 * (`notificationTypeValidator` in convex/schema.ts, `NOTIFICATION_TYPES`
 * below, two translations) and one call of `notifyMachine` where the
 * incident is created. A session failed before its machine armed it
 * (ANH-144) is a call of `notifyMachine(..., "session_failed", ...)` there.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx } from "../_generated/server";
import { organizationRoleOf } from "./auth";
import {
  alertEmail,
  emailSettings,
  queueAlertEmail,
  type NotificationType,
} from "./notificationEmail";

export type { NotificationType };

/**
 * Each kind of alert: what a new event folds into, and whether it goes by
 * e-mail too for an account that has not chosen (`notification_settings`).
 *
 * - "machine": a state of the machine. A new event folds into the
 *   recipient's unread alert of that kind for that machine.
 * - "session": something that happened in one session. It folds into the
 *   recipient's unread alert of that kind for that session only.
 */
export const NOTIFICATION_TYPES = {
  machine_offline: { foldsBy: "machine", emailByDefault: false },
  session_failed: { foldsBy: "session", emailByDefault: true },
  drive_fault: { foldsBy: "machine", emailByDefault: true },
  safety_verdict: { foldsBy: "session", emailByDefault: true },
} as const satisfies Record<
  NotificationType,
  { foldsBy: "machine" | "session"; emailByDefault: boolean }
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
    const role = await organizationRoleOf(
      ctx,
      link.gestionnaireId,
      organizationId,
    );
    if (role === "gestionnaire") ids.add(link.gestionnaireId);
  }
  const users: Doc<"users">[] = [];
  for (const id of ids) {
    const user = await ctx.db.get(id);
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

/**
 * Alert the recipients of this machine (`recipientsOf`) of one event, or of
 * several of one kind. For each recipient, the event folds into their unread
 * alert of that kind (`NOTIFICATION_TYPES`); without one, a new alert is
 * written, and its e-mail queued when the deployment sends e-mails, the
 * recipient wants this kind by e-mail and has an address. Returns how many
 * alerts were written.
 *
 * Nothing for a deleted machine, one without organisation, or one whose
 * organisation was deleted.
 */
export async function notifyMachine(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  type: NotificationType,
  event: MachineEvent = {},
): Promise<number> {
  const organizationId = machine.organizationId;
  if (machine.isDeleted || organizationId === undefined) return 0;
  const organization = await ctx.db.get(organizationId);
  if (!organization || organization.deletedAt !== undefined) return 0;

  const now = Date.now();
  const count = event.count ?? 1;
  const detail =
    event.detail !== undefined && event.detail !== null && CODE.test(event.detail)
      ? event.detail
      : undefined;
  const settings = emailSettings();
  let written = 0;
  for (const user of await recipientsOf(ctx, machine, organizationId)) {
    const unread = await ctx.db
      .query("notifications")
      .withIndex("by_user_and_machine_and_type_and_read", (q) =>
        q
          .eq("userId", user._id)
          .eq("machineId", machine._id)
          .eq("type", type)
          .eq("read", false),
      )
      .collect();
    const same = unread.find(
      (row) =>
        row.organizationId === organizationId &&
        (NOTIFICATION_TYPES[type].foldsBy === "machine" ||
          row.sessionId === event.sessionId),
    );
    if (same) {
      await ctx.db.patch(same._id, {
        count: same.count + count,
        updatedAt: now,
        detail: foldedDetail(type, same.detail, detail),
        sessionId: same.sessionId ?? event.sessionId,
      });
      continue;
    }
    const notificationId = await ctx.db.insert("notifications", {
      organizationId,
      userId: user._id,
      type,
      machineId: machine._id,
      sessionId: event.sessionId,
      detail,
      count,
      createdAt: now,
      updatedAt: now,
      read: false,
    });
    written++;
    if (settings === null || user.email.trim() === "") continue;
    const choices = emailChoices(
      await ctx.db
        .query("notification_settings")
        .withIndex("by_user", (q) => q.eq("userId", user._id))
        .unique(),
    );
    if (!choices[type]) continue;
    const emailId = await queueAlertEmail(
      ctx,
      settings,
      user.email.trim(),
      alertEmail(type, user.language, machine.name, settings.siteUrl),
      `notification:${notificationId}`,
    );
    await ctx.db.patch(notificationId, { emailId });
  }
  return written;
}

/** How long an alert is kept, read or not. */
export const NOTIFICATION_RETENTION_MS = 90 * 24 * 60 * 60 * 1000;
