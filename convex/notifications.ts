/**
 * The alerts of the gestionnaires and organisation admins (ANH-90), as the
 * site reads them: the bell of the header, the alerts page, and each
 * account's choice of alerts sent by e-mail too.
 *
 * The alerts themselves are written by the server where it detects an event
 * (convex/lib/notifications.ts). Here a caller reads, and marks as read, only
 * their own alerts in the organisation of the call, and only those about a
 * machine they can still reach: a gestionnaire removed
 * from a machine no longer sees its alerts. A patient has none.
 */
import { limitUser } from "./lib/rateLimits";
import { v, type Infer } from "convex/values";
import {
  internalMutation,
  mutation,
  query,
  type MutationCtx,
} from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import {
  STAFF_ROLES,
  canAccessSession,
  getCurrentUserOrThrow,
  requireRole,
} from "./lib/auth";
import { refusal } from "./lib/errors";
import {
  NOTIFICATION_RETENTION_MS,
  emailChoices,
  notifyMachine,
  notifyRecipient,
  retryDelivery,
} from "./lib/notifications";
import { emailSettings } from "./lib/notificationEmail";
import { notificationTypeValidator } from "./schema";
import {
  continueMarkAllReadHandler,
  markAllReadHandler,
  notificationVisibility,
  recentVisible,
  staffOrNull,
  unreadCountHandler,
} from "./lib/notificationVisibility";

export {
  LIST_LIMIT,
  UNREAD_COUNT_LIMIT,
  MARK_ALL_READ_LIMIT,
} from "./lib/notificationVisibility";

const notificationRow = v.object({
  _id: v.id("notifications"),
  type: notificationTypeValidator,
  machineId: v.id("machines"),
  machineName: v.string(),
  machineDeleted: v.boolean(),
  // Only when the caller may read the session.
  sessionId: v.optional(v.id("sessions")),
  // A machine-side code: the drive's fault mnemonic, or a verdict's action.
  detail: v.optional(v.string()),
  // Why a failed session ended, as the session holds it now: what the page
  // of the session shows to the same people. `reasonCode` is its stable
  // code when it has one (a launch refused before its machine armed it,
  // ANH-144), which the site words in place of the sentence.
  reason: v.optional(v.string()),
  reasonCode: v.optional(v.string()),
  count: v.number(),
  createdAt: v.number(),
  updatedAt: v.number(),
  read: v.boolean(),
});

/**
 * The caller's most recent alerts in the organisation of the call, newest
 * change first, `LIST_LIMIT` at most. Empty for a patient.
 */
export const list = query({
  args: {},
  returns: v.array(notificationRow),
  handler: async (ctx) => {
    const caller = staffOrNull(await getCurrentUserOrThrow(ctx));
    if (caller === null) return [];
    const rows = await recentVisible(ctx, caller);
    const result = [];
    for (const row of rows) {
      const machine = await ctx.db.get(row.machineId);
      const session =
        row.sessionId === undefined ? null : await ctx.db.get(row.sessionId);
      const readable =
        session !== null && (await canAccessSession(ctx, session, caller));
      result.push({
        _id: row._id,
        type: row.type,
        machineId: row.machineId,
        machineName: machine?.name ?? "-",
        machineDeleted: machine === null || machine.isDeleted === true,
        sessionId: readable ? session._id : undefined,
        detail: row.detail,
        reason:
          readable && row.type === "session_failed"
            ? session.endReason
            : undefined,
        reasonCode:
          readable && row.type === "session_failed"
            ? session.endCode
            : undefined,
        count: row.count,
        createdAt: row.createdAt,
        updatedAt: row.updatedAt,
        read: row.read,
      });
    }
    return result;
  },
});

/**
 * How many of the caller's alerts are unread, `UNREAD_COUNT_LIMIT` at most;
 * null when the caller receives no alerts: signed out, without an accepted
 * organisation, or a patient. Never throws: the header reads it on every
 * page, the organisation gate included.
 */
export const unreadCount = query({
  args: {},
  returns: v.union(v.number(), v.null()),
  handler: unreadCountHandler,
});

/** Mark one of the caller's alerts as read. */
export const markRead = mutation({
  args: { notificationId: v.id("notifications") },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, STAFF_ROLES);
    const row = await ctx.db.get(args.notificationId);
    const visibility = await notificationVisibility(ctx, caller);
    // Another account's alert, or one of another organisation, is an alert
    // that does not exist.
    if (!row || !visibility.allows(row)) {
      throw refusal("notification_not_found");
    }
    if (!row.read) await ctx.db.patch(row._id, { read: true });
    return null;
  },
});

/** Mark visible alerts read in bounded transactions, continuing without a new click. */
export const markAllRead = mutation({
  args: {},
  returns: v.object({ marked: v.number() }),
  handler: markAllReadHandler,
});

export const continueMarkAllRead = internalMutation({
  args: { userId: v.id("users"), organizationId: v.id("organizations") },
  returns: v.object({ marked: v.number() }),
  handler: continueMarkAllReadHandler,
});

const emailChoicesValidator = v.object({
  machine_offline: v.boolean(),
  session_failed: v.boolean(),
  drive_fault: v.boolean(),
  safety_verdict: v.boolean(),
});

/**
 * Which alerts the caller also receives by e-mail, defaults included, and
 * whether this deployment sends e-mails at all (`emailAvailable`). Null for a
 * patient.
 */
export const getSettings = query({
  args: {},
  returns: v.union(
    v.object({ emailAvailable: v.boolean(), email: emailChoicesValidator }),
    v.null(),
  ),
  handler: async (ctx) => {
    const caller = staffOrNull(await getCurrentUserOrThrow(ctx));
    if (caller === null) return null;
    const settings = await ctx.db
      .query("notification_settings")
      .withIndex("by_user", (q) => q.eq("userId", caller._id))
      .unique();
    return {
      emailAvailable: emailSettings() !== null,
      email: emailChoices(settings),
    };
  },
});

/** Choose whether one kind of alert is also sent to the caller by e-mail. */
export const updateSettings = mutation({
  args: { type: notificationTypeValidator, email: v.boolean() },
  returns: v.null(),
  handler: async (ctx, args) => {
    await limitUser(ctx);
    const caller = await requireRole(ctx, STAFF_ROLES);
    const settings = await ctx.db
      .query("notification_settings")
      .withIndex("by_user", (q) => q.eq("userId", caller._id))
      .unique();
    const email: Doc<"notification_settings">["email"] = {
      ...settings?.email,
    };
    email[args.type] = args.email;
    const now = Date.now();
    if (settings === null) {
      await ctx.db.insert("notification_settings", {
        userId: caller._id,
        email,
        updatedAt: now,
      });
    } else {
      await ctx.db.patch(settings._id, { email, updatedAt: now });
    }
    return null;
  },
});

/** Validate a scheduled delivery and the bounded retry's attempt and failed recipients. */
export const deliveryArgs = v.object({
  machineId: v.id("machines"),
  type: notificationTypeValidator,
  sessionId: v.optional(v.id("sessions")),
  detail: v.optional(v.string()),
  count: v.number(),
  recipientIds: v.optional(v.array(v.id("users"))),
  attempt: v.optional(v.number()),
});

/** Load the machine and deliver its alert, retrying a failed lookup within the bound. */
export async function deliverHandler(
  ctx: MutationCtx,
  args: Infer<typeof deliveryArgs>,
): Promise<number> {
  let machine: Doc<"machines"> | null;
  try {
    machine = await ctx.db.get(args.machineId);
  } catch {
    await retryDelivery(ctx, args.machineId, args.type, args, args);
    return 0;
  }
  if (machine === null) return 0;
  return await notifyMachine(ctx, machine, args.type, args, args);
}

/**
 * Write the alert `scheduleAlert` asked for (convex/lib/notifications.ts), in
 * a transaction of its own, or retry only its failed recipients. Do nothing for
 * a machine whose row is gone. Return how many new alerts were written.
 */
export const deliver = internalMutation({
  args: deliveryArgs.fields,
  returns: v.number(),
  handler: deliverHandler,
});

/** Validate one recipient's subtransaction without carrying delivery retry state. */
export const recipientDeliveryArgs = deliveryArgs
  .omit("recipientIds", "attempt")
  .extend({ userId: v.id("users") });

/** Recheck that the machine and recipient exist before writing this recipient's alert. */
export async function deliverRecipientHandler(
  ctx: MutationCtx,
  args: Infer<typeof recipientDeliveryArgs>,
): Promise<number> {
  const machine = await ctx.db.get(args.machineId);
  const user = await ctx.db.get(args.userId);
  if (!machine || !user || user.clerkDeletedAt !== undefined) return 0;
  return await notifyRecipient(ctx, machine, user, args.type, args);
}

/** Isolate one recipient's writes so their failure cannot undo another recipient's alert. */
export const deliverRecipient = internalMutation({
  args: recipientDeliveryArgs.fields,
  returns: v.number(),
  handler: deliverRecipientHandler,
});

/**
 * Delete the alerts not changed for `NOTIFICATION_RETENTION_MS`, read or
 * not, 500 at a time. Run every day (`convex/crons.ts`); returns how many it
 * deleted, and runs again at once while there are more.
 */
export const purge = internalMutation({
  args: {},
  returns: v.object({ deleted: v.number() }),
  handler: async (ctx) => {
    await ctx.scheduler.runAfter(
      0,
      internal.notificationEmails.abandonPending,
      {},
    );
    const cutoff = Date.now() - NOTIFICATION_RETENTION_MS;
    const old = await ctx.db
      .query("notifications")
      .withIndex("by_updated", (q) => q.lt("updatedAt", cutoff))
      .take(500);
    for (const row of old) await ctx.db.delete(row._id);
    if (old.length === 500) {
      await ctx.scheduler.runAfter(0, internal.notifications.purge, {});
    }
    return { deleted: old.length };
  },
});
