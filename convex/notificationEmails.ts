import { v } from "convex/values";
import { internal } from "./_generated/api";
import {
  internalAction,
  internalMutation,
  internalQuery,
} from "./_generated/server";
import { alertEmail, emailSettings } from "./lib/notificationEmail";
import { notificationTypeValidator } from "./schema";

// All attempts must fit inside Resend's 24-hour idempotency window.
export const EMAIL_MAX_ATTEMPTS = 5;
export const EMAIL_RETRY_WINDOW_MS = 23 * 60 * 60 * 1_000;
export const EMAIL_RETRY_DELAYS_MS = [
  60_000, 300_000, 1_800_000, 7_200_000,
] as const;
export const EMAIL_REQUEST_TIMEOUT_MS = 30_000;

const attemptArgs = {
  notificationId: v.id("notifications"),
  attempt: v.number(),
};

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
    return {
      to: user.email.trim(),
      language: user.language,
      machineName: machine?.name ?? "-",
      type: row.type,
    };
  },
});

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
        code: "email_unavailable",
      });
      return null;
    }
    let outcome: "sent" | "retry" | "failed" = "retry";
    let code = "email_network_error";
    let resendId: string | undefined;
    try {
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
        signal: AbortSignal.timeout(EMAIL_REQUEST_TIMEOUT_MS),
      });
      code = `email_http_${response.status}`;
      outcome =
        response.status === 429 || response.status >= 500 ? "retry" : "failed";
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
