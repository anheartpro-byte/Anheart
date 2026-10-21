/**
 * Send alert e-mails through Resend's REST API without storing credentials.
 * `notifyRecipient` schedules the first action after writing a new alert;
 * `finishAttempt` retries 429, concurrent idempotent requests, 5xx and network errors.
 * Other responses are final, including a success without a usable message ID.
 * Each action reads the API key at sending time and never stores or returns it.
 * New attempts stop after 23 hours to stay within Resend's 24-hour idempotency
 * window, using the same key for every attempt of an alert.
 */
import { v } from "convex/values";
import { internal } from "./_generated/api";
import {
  internalAction,
  internalMutation,
  internalQuery,
} from "./_generated/server";
import { alertEmail, emailSettings } from "./lib/notificationEmail";
import { isMachineRecipient } from "./lib/notifications";
import { notificationTypeValidator } from "./schema";

/** Allow at most five claimed attempts for one alert e-mail. */
export const EMAIL_MAX_ATTEMPTS = 5;
/** Stop starting attempts after 23 hours, before the 24-hour idempotency limit. */
export const EMAIL_RETRY_WINDOW_MS = 23 * 60 * 60 * 1_000;
/** Space the four possible retries increasingly to avoid immediate repeat failures. */
export const EMAIL_RETRY_DELAYS_MS = [
  60_000, 300_000, 1_800_000, 7_200_000,
] as const;
/** Abort a request after 30 seconds when the runtime supports a deadline. */
export const EMAIL_REQUEST_TIMEOUT_MS = 30_000;
/** Close abandoned attempts after the longest retry delay (2 h) plus a 1 h margin. */
export const EMAIL_ABANDONED_AFTER_MS = 3 * 60 * 60 * 1_000;
/** Bound the number of e-mails closed by one cleanup transaction. */
export const EMAIL_ABANDONED_BATCH_SIZE = 500;

const attemptArgs = {
  notificationId: v.id("notifications"),
  attempt: v.number(),
};

/** Recheck pending state and the current recipient just before sending an e-mail. */
export const pending = internalQuery({
  args: { notificationId: v.id("notifications") },
  returns: v.union(
    v.null(),
    v.object({
      to: v.string(),
      language: v.union(v.literal("fr"), v.literal("en")),
      machineName: v.string(),
      type: notificationTypeValidator,
    }),
  ),
  handler: async (ctx, { notificationId }) => {
    const row = await ctx.db.get(notificationId);
    if (row?.email?.status !== "pending") return null;
    const user = await ctx.db.get(row.userId);
    if (!user || user.clerkDeletedAt !== undefined || user.email.trim() === "")
      return null;
    const machine = await ctx.db.get(row.machineId);
    if (
      !machine ||
      machine.isDeleted === true ||
      machine.organizationId !== row.organizationId
    )
      return null;
    const organization = await ctx.db.get(row.organizationId);
    if (!organization || organization.deletedAt !== undefined) return null;
    if (
      !(await isMachineRecipient(
        ctx,
        machine._id,
        row.organizationId,
        user._id,
      ))
    )
      return null;
    return {
      to: user.email.trim(),
      language: user.language,
      machineName: machine.name,
      type: row.email.type ?? row.type,
    };
  },
});

/** Close stalled pending e-mails from the daily purge without extending alert retention. */
export const abandonPending = internalMutation({
  args: {},
  returns: v.object({ failed: v.number() }),
  handler: async (ctx) => {
    const now = Date.now();
    const stale = await ctx.db
      .query("notifications")
      .withIndex("by_email_status_and_updated", (q) =>
        q
          .eq("email.status", "pending")
          .lt("email.updatedAt", now - EMAIL_ABANDONED_AFTER_MS),
      )
      .take(EMAIL_ABANDONED_BATCH_SIZE);
    for (const row of stale) {
      await ctx.db.patch(row._id, {
        email: {
          ...row.email!,
          status: "failed",
          updatedAt: now,
          failureCode: "email_abandoned",
        },
      });
    }
    if (stale.length === EMAIL_ABANDONED_BATCH_SIZE) {
      await ctx.scheduler.runAfter(
        0,
        internal.notificationEmails.abandonPending,
        {},
      );
    }
    return { failed: stale.length };
  },
});

/**
 * Claim the expected attempt once so duplicate or stale actions cannot send it.
 * Record exhaustion instead of starting beyond the attempt or 23-hour limit.
 */
export const beginAttempt = internalMutation({
  args: attemptArgs,
  returns: v.boolean(),
  handler: async (ctx, { notificationId, attempt }) => {
    const row = await ctx.db.get(notificationId);
    const email = row?.email;
    if (!email || email.status !== "pending" || email.attempts !== attempt)
      return false;
    const now = Date.now();
    if (
      attempt >= EMAIL_MAX_ATTEMPTS ||
      now >= email.queuedAt + EMAIL_RETRY_WINDOW_MS
    ) {
      await ctx.db.patch(notificationId, {
        email: {
          ...email,
          status: "failed",
          updatedAt: now,
          failureCode: "email_retry_exhausted",
        },
      });
      return false;
    }
    await ctx.db.patch(notificationId, {
      email: { ...email, attempts: attempt + 1, updatedAt: now },
    });
    return true;
  },
});

/**
 * Apply only the current claimed attempt's result, ignoring stale results.
 * Schedule a retry only within both bounds; otherwise keep a final status and
 * safe code or message ID, without retaining a response body or exception text.
 */
export const finishAttempt = internalMutation({
  args: {
    ...attemptArgs,
    outcome: v.union(
      v.literal("sent"),
      v.literal("retry"),
      v.literal("failed"),
    ),
    code: v.optional(v.string()),
    resendId: v.optional(v.string()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const row = await ctx.db.get(args.notificationId);
    const email = row?.email;
    if (!email || email.status !== "pending" || email.attempts !== args.attempt)
      return null;
    const now = Date.now();
    const delay = EMAIL_RETRY_DELAYS_MS[email.attempts - 1];
    if (
      args.outcome === "retry" &&
      email.attempts < EMAIL_MAX_ATTEMPTS &&
      now + delay < email.queuedAt + EMAIL_RETRY_WINDOW_MS
    ) {
      try {
        await ctx.scheduler.runAfter(delay, internal.notificationEmails.send, {
          notificationId: row._id,
          attempt: email.attempts,
        });
        await ctx.db.patch(row._id, {
          email: { ...email, updatedAt: now, failureCode: args.code },
        });
        return null;
      } catch {
        // A scheduling failure must leave a final trace, not a pending e-mail.
      }
    }
    await ctx.db.patch(row._id, {
      email: {
        ...email,
        updatedAt: now,
        status: args.outcome === "sent" ? "sent" : "failed",
        resendId: args.resendId,
        failureCode: args.outcome === "sent" ? undefined : args.code,
      },
    });
    return null;
  },
});

/**
 * Send the action queued by `notifyRecipient` or a retry from `finishAttempt`.
 * Read the key from the environment only here, pass it only to Resend, and use
 * one idempotency key per alert across retries of 429, 5xx and network errors.
 * Final responses are not retried, and the request deadline is optional when
 * neither the native timeout nor a controller with a timer is available.
 */
export const send = internalAction({
  args: attemptArgs,
  returns: v.null(),
  handler: async (ctx, args) => {
    const started: boolean = await ctx.runMutation(
      internal.notificationEmails.beginAttempt,
      args,
    );
    if (!started) return null;
    const settings = emailSettings();
    const recipient = await ctx.runQuery(internal.notificationEmails.pending, {
      notificationId: args.notificationId,
    });
    const attempt = args.attempt + 1;
    if (settings === null || recipient === null) {
      await ctx.runMutation(internal.notificationEmails.finishAttempt, {
        ...args,
        attempt,
        outcome: "failed",
        code: settings === null ? "email_unavailable" : "email_recipient_gone",
      });
      return null;
    }
    let outcome: "sent" | "retry" | "failed";
    let code: string;
    let resendId: string | undefined;
    let timeout: ReturnType<typeof setTimeout> | undefined;
    try {
      let signal: AbortSignal | undefined;
      if (
        typeof AbortSignal !== "undefined" &&
        typeof AbortSignal.timeout === "function"
      ) {
        signal = AbortSignal.timeout(EMAIL_REQUEST_TIMEOUT_MS);
      } else if (
        typeof AbortController !== "undefined" &&
        typeof setTimeout === "function"
      ) {
        const controller = new AbortController();
        timeout = setTimeout(
          () => controller.abort(),
          EMAIL_REQUEST_TIMEOUT_MS,
        );
        signal = controller.signal;
      }
      const response = await fetch("https://api.resend.com/emails", {
        method: "POST",
        headers: {
          Authorization: `Bearer ${settings.apiKey}`,
          "Content-Type": "application/json",
          "Idempotency-Key": `notification:${args.notificationId}`,
        },
        body: JSON.stringify({
          from: settings.from,
          to: [recipient.to],
          ...alertEmail(
            recipient.type,
            recipient.language,
            recipient.machineName,
            settings.siteUrl,
          ),
        }),
        signal,
      });
      code = `email_http_${response.status}`;
      outcome =
        response.status === 429 || response.status >= 500 ? "retry" : "failed";
      if (response.status === 409) {
        try {
          const body: unknown = await response.json();
          if (typeof body === "object" && body !== null && "name" in body) {
            if (body.name === "concurrent_idempotent_requests") {
              outcome = "retry";
            } else if (body.name === "invalid_idempotent_request") {
              code = "email_idempotency_conflict";
            }
          }
        } catch {
          // An unreadable conflict body remains a final email_http_409.
        }
      }
      if (response.ok) {
        const body: unknown = await response.json();
        if (
          typeof body === "object" &&
          body !== null &&
          "id" in body &&
          typeof body.id === "string"
        ) {
          resendId = body.id;
          outcome = "sent";
        } else {
          code = "email_invalid_response";
        }
      }
    } catch {
      // Neither an exception nor a response body may persist the API key.
      outcome = "retry";
      code = "email_network_error";
    } finally {
      if (timeout !== undefined) clearTimeout(timeout);
    }
    await ctx.runMutation(internal.notificationEmails.finishAttempt, {
      ...args,
      attempt,
      outcome,
      code,
      resendId,
    });
    return null;
  },
});
