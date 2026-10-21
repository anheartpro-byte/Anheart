import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import { internalMutation } from "./_generated/server";
import {
  deliverHandler,
  deliveryArgs,
  deliverRecipientHandler,
  recipientDeliveryArgs,
} from "./notifications";
import { alertEmail } from "./lib/notificationEmail";
import {
  DELIVERY_MAX_ATTEMPTS,
  DELIVERY_RETRY_DELAYS_MS,
  NOTIFICATION_RETENTION_MS,
  notifyMachine,
  notifyRecipient,
} from "./lib/notifications";
import {
  EMAIL_ABANDONED_AFTER_MS,
  EMAIL_ABANDONED_BATCH_SIZE,
  EMAIL_MAX_ATTEMPTS,
  EMAIL_REQUEST_TIMEOUT_MS,
  EMAIL_RETRY_DELAYS_MS,
  EMAIL_RETRY_WINDOW_MS,
} from "./notificationEmails";
import schema from "./schema";
import { as, modules, NOW, seedLaunchWorld, type World } from "./test.setup";

// Built at run time: the secret scanner must not see a key-shaped literal.
const SECRET = ["re", "sentinel", "resend", "key"].join("_");
const FROM = "Gaura <alertes@example.invalid>";
const SITE = "https://site.example.invalid";
const network = vi.fn<typeof fetch>();

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.stubEnv("RESEND_API_KEY", SECRET);
  vi.stubEnv("NOTIFICATION_EMAIL_FROM", FROM);
  vi.stubEnv("SITE_URL", SITE);
  network
    .mockReset()
    .mockImplementation(async () => Response.json({ id: "email_synthetic" }));
  vi.stubGlobal("fetch", network);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

async function rows(w: World) {
  return await w.t.run((ctx) => ctx.db.query("notifications").collect());
}

async function queue(w: World) {
  await w.t.run((ctx) => ctx.db.patch(w.orgAdmin, { email: "" }));
  await w.t.mutation(internal.notifications.deliver, {
    machineId: w.machine,
    type: "drive_fault",
    count: 1,
  });
  const row = (await rows(w)).find((r) => r.userId === w.manager);
  if (!row) throw new Error("No manager alert");
  return row;
}

async function email(w: World, row: Doc<"notifications">) {
  return (await w.t.run((ctx) => ctx.db.get(row._id)))?.email;
}

async function drain(w: World) {
  await w.t.finishAllScheduledFunctions(vi.runAllTimers);
}

describe("isolated delivery (T02-a, T02-b)", () => {
  it("keeps the site alert when duplicate settings fail, and queues the other recipient", async () => {
    const w = await seedLaunchWorld(modules);
    await w.t.run(async (ctx) => {
      for (let i = 0; i < 2; i++)
        await ctx.db.insert("notification_settings", {
          userId: w.manager,
          email: {},
          updatedAt: NOW,
        });
    });
    await w.t.mutation(internal.notifications.deliver, {
      machineId: w.machine,
      type: "drive_fault",
      count: 1,
    });
    expect(
      (await rows(w)).find((r) => r.userId === w.manager)?.email,
    ).toMatchObject({
      status: "failed",
      attempts: 0,
      failureCode: "email_queue_failed",
    });
    expect(
      (await rows(w)).find((r) => r.userId === w.orgAdmin)?.email,
    ).toMatchObject({ status: "pending", attempts: 0 });
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
  });

  it.each([false, true])(
    "retries only failed recipient writes, with one safe final log (recover=%s)",
    async (recover) => {
      const attempted: number[] = [];
      const error = vi.spyOn(console, "error").mockImplementation(() => {});
      const w: World = await seedLaunchWorld({
        ...modules,
        "./notifications.ts": async () => ({
          ...(await import("./notifications")),
          deliverRecipient: internalMutation({
            args: recipientDeliveryArgs.fields,
            handler: async (ctx, args) => {
              const insert = ctx.db.insert.bind(ctx.db);
              vi.spyOn(ctx.db, "insert").mockImplementation(
                async (...params: Parameters<typeof ctx.db.insert>) => {
                  const [table, value] = params;
                  if (
                    table === "notifications" &&
                    "userId" in value &&
                    value.userId === manager
                  ) {
                    attempted.push(Date.now());
                    if (!recover || attempted.length < 3)
                      throw new Error(
                        "FC_191_PASSENGER_SECRET_ADDRESS@example.invalid",
                      );
                  }
                  return await insert(...params);
                },
              );
              return await deliverRecipientHandler(ctx, args);
            },
          }),
        }),
      });
      const manager: World["manager"] = w.manager;
      await w.t.mutation(internal.notifications.deliver, {
        machineId: w.machine,
        type: "drive_fault",
        count: 1,
        detail: "PRIVATE sentence FC 191",
      });
      await drain(w);
      expect(attempted).toEqual([
        NOW,
        NOW + DELIVERY_RETRY_DELAYS_MS[0],
        NOW + DELIVERY_RETRY_DELAYS_MS[0] + DELIVERY_RETRY_DELAYS_MS[1],
      ]);
      expect(attempted).toHaveLength(DELIVERY_MAX_ATTEMPTS);
      expect((await rows(w)).find((r) => r.userId === w.orgAdmin)?.count).toBe(
        1,
      );
      if (recover) {
        expect((await rows(w)).find((r) => r.userId === manager)?.count).toBe(
          1,
        );
        expect(error).not.toHaveBeenCalled();
      } else {
        expect(await rows(w)).toHaveLength(1);
        expect(error.mock.calls).toEqual([
          [
            {
              code: "notification_delivery_failed",
              machineId: w.machine,
              type: "drive_fault",
              sessionId: undefined,
              recipientIds: [manager],
              attempts: 3,
            },
          ],
        ]);
        expect(JSON.stringify(error.mock.calls)).not.toMatch(
          /PRIVATE|FC_191|191|PASSENGER|@|sentence/,
        );
      }
    },
  );

  it("retries a failure before the recipient loop three times and logs only codes and IDs", async () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    let attempts = 0;
    const w: World = await seedLaunchWorld({
      ...modules,
      "./notifications.ts": async () => ({
        ...(await import("./notifications")),
        deliver: internalMutation({
          args: deliveryArgs.fields,
          handler: async (ctx, args) => {
            const get = ctx.db.get.bind(ctx.db);
            vi.spyOn(ctx.db, "get").mockImplementation(
              async (...params: Parameters<typeof ctx.db.get>) => {
                if (params[0] === machine) {
                  attempts++;
                  throw new Error(SECRET);
                }
                return await get(...params);
              },
            );
            return await deliverHandler(ctx, args);
          },
        }),
      }),
    });
    const machine: World["machine"] = w.machine;
    await w.t.mutation(internal.notifications.deliver, {
      machineId: machine,
      type: "drive_fault",
      count: 1,
    });
    await drain(w);
    expect(attempts).toBe(3);
    expect(await rows(w)).toEqual([]);
    expect(error).toHaveBeenCalledTimes(1);
    expect(JSON.stringify(error.mock.calls)).not.toContain(SECRET);
  });

  it("retries a recipient lookup failure before writing any alert, defaulting to one event", async () => {
    const w = await seedLaunchWorld(modules);
    await w.t.run(async (ctx) => {
      const machine = await ctx.db.get(w.machine);
      if (!machine) throw new Error("No machine");
      vi.spyOn(ctx.db, "query").mockImplementationOnce(() => {
        throw new Error(SECRET);
      });
      expect(await notifyMachine(ctx, machine, "drive_fault")).toBe(0);
    });
    expect(await rows(w)).toEqual([]);
    await drain(w);
    expect((await rows(w)).map((row) => row.count)).toEqual([1, 1]);
  });

  it("defaults direct recipient events to one and ignores a machine without an organisation", async () => {
    const w = await seedLaunchWorld(modules);
    await w.t.run(async (ctx) => {
      const machine = await ctx.db.get(w.machine);
      const user = await ctx.db.get(w.manager);
      if (!machine || !user) throw new Error("Missing recipient");
      expect(
        await notifyRecipient(
          ctx,
          { ...machine, organizationId: undefined },
          user,
          "drive_fault",
          {},
        ),
      ).toBe(0);
      expect(await notifyRecipient(ctx, machine, user, "drive_fault", {})).toBe(
        1,
      );
      expect(await notifyRecipient(ctx, machine, user, "drive_fault", {})).toBe(
        0,
      );
    });
    expect((await rows(w)).map((row) => row.count)).toEqual([2]);
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
  });

  it("skips a subtransaction when the recipient or machine disappeared, or Clerk deleted the recipient", async () => {
    const w = await seedLaunchWorld(modules);
    const args = {
      machineId: w.machine,
      userId: w.manager,
      type: "drive_fault" as const,
      count: 1,
    };
    await w.t.run((ctx) => ctx.db.patch(w.manager, { clerkDeletedAt: NOW }));
    expect(
      await w.t.mutation(internal.notifications.deliverRecipient, args),
    ).toBe(0);
    await w.t.run((ctx) => ctx.db.delete(w.manager));
    expect(
      await w.t.mutation(internal.notifications.deliverRecipient, args),
    ).toBe(0);
    await w.t.run((ctx) => ctx.db.delete(w.machine));
    expect(
      await w.t.mutation(internal.notifications.deliverRecipient, {
        ...args,
        userId: w.orgAdmin,
      }),
    ).toBe(0);
    expect(await rows(w)).toEqual([]);
  });
});

describe("REST sending without persisted credentials (T02-c)", () => {
  it("uses the exact REST contract, scans every app table and scheduled args, and never logs the key", async () => {
    const logs = ["log", "info", "warn", "error", "debug"] as const;
    const spies = logs.map((method) =>
      vi.spyOn(console, method).mockImplementation(() => {}),
    );
    const w = await seedLaunchWorld(modules);
    await w.t.run((ctx) => ctx.db.patch(w.manager, { language: "fr" }));
    const row = await queue(w);
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    const [url, init] = network.mock.calls[0];
    expect(url).toBe("https://api.resend.com/emails");
    expect(init?.method).toBe("POST");
    expect(init?.signal).toBeInstanceOf(AbortSignal);
    expect(new Headers(init?.headers).get("Authorization")).toBe(
      `Bearer ${SECRET}`,
    );
    expect(new Headers(init?.headers).get("Idempotency-Key")).toBe(
      `notification:${row._id}`,
    );
    const user = await w.t.run((ctx) => ctx.db.get(w.manager));
    expect(JSON.parse(String(init?.body))).toEqual({
      from: FROM,
      to: [user?.email],
      ...alertEmail("drive_fault", "fr", "Synthetic machine", SITE),
    });
    const stored = await w.t.run(async (ctx) => {
      const documents = await Promise.all(
        (Object.keys(schema.tables) as (keyof typeof schema.tables)[]).map(
          (table) => ctx.db.query(table).collect(),
        ),
      );
      const scheduled = await ctx.db.system
        .query("_scheduled_functions")
        .collect();
      return { documents, scheduled };
    });
    expect(stored.scheduled.length).toBeGreaterThan(0);
    expect(JSON.stringify(stored)).not.toContain(SECRET);
    expect(JSON.stringify(spies.map((spy) => spy.mock.calls))).not.toContain(
      SECRET,
    );
    expect(await email(w, row)).toMatchObject({
      status: "sent",
      attempts: 1,
      resendId: "email_synthetic",
    });
    expect((await rows(w)).every((r) => r.emailId === undefined)).toBe(true);
  });

  it.each(["timeout", "AbortSignal", "AbortController", "setTimeout"])(
    "sends once with an optional request deadline when %s is unavailable",
    async (missing) => {
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      const scheduleTimeout = setTimeout;
      vi.stubGlobal(
        "AbortSignal",
        missing === "AbortSignal" ? undefined : { timeout: undefined },
      );
      if (missing === "AbortController")
        vi.stubGlobal("AbortController", undefined);
      if (missing === "setTimeout") vi.stubGlobal("setTimeout", undefined);
      await w.t.action(internal.notificationEmails.send, {
        notificationId: row._id,
        attempt: 0,
      });
      vi.stubGlobal("setTimeout", scheduleTimeout);
      expect(network).toHaveBeenCalledTimes(1);
      expect(await email(w, row)).toMatchObject({
        status: "sent",
        attempts: 1,
        resendId: "email_synthetic",
      });
      const signal = network.mock.calls[0][1]?.signal;
      if (missing === "timeout" || missing === "AbortSignal") {
        expect(signal?.aborted).toBe(false);
        await vi.advanceTimersByTimeAsync(EMAIL_REQUEST_TIMEOUT_MS);
        expect(signal?.aborted).toBe(false);
      } else {
        expect(signal).toBeUndefined();
      }
      await drain(w);
      expect(network).toHaveBeenCalledTimes(1);
    },
  );

  it("aborts a stalled request with the fallback controller and retries it", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.stubGlobal("AbortSignal", { timeout: undefined });
    let started: () => void = () => {};
    const requested = new Promise<void>((resolve) => {
      started = resolve;
    });
    network.mockImplementationOnce(
      async (_url, init) =>
        await new Promise<Response>((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () =>
            reject(new Error("Timeout")),
          );
          started();
        }),
    );
    const sending = w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: 0,
    });
    await requested;
    await vi.advanceTimersByTimeAsync(EMAIL_REQUEST_TIMEOUT_MS);
    await sending;
    expect(network.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(await email(w, row)).toMatchObject({
      status: "pending",
      attempts: 1,
      failureCode: "email_network_error",
    });
    await drain(w);
    expect(network).toHaveBeenCalledTimes(2);
    expect(await email(w, row)).toMatchObject({ status: "sent", attempts: 2 });
  });
});

describe("bounded email retries (T02-d)", () => {
  it.each([429, 500, "network"] as const)(
    "retries %s five times with increasing delays then records failure",
    async (failure) => {
      const times: number[] = [];
      network.mockImplementation(async () => {
        times.push(Date.now());
        if (failure === "network") throw new Error(SECRET);
        return Response.json({ message: SECRET }, { status: failure });
      });
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      await drain(w);
      expect(times).toHaveLength(EMAIL_MAX_ATTEMPTS);
      expect(times.slice(1).map((t, i) => t - times[i])).toEqual(
        EMAIL_RETRY_DELAYS_MS,
      );
      expect(times[times.length - 1] - times[0]).toBeLessThan(
        EMAIL_RETRY_WINDOW_MS,
      );
      expect(EMAIL_RETRY_WINDOW_MS).toBeLessThan(24 * 60 * 60 * 1000);
      expect(await email(w, row)).toMatchObject({
        status: "failed",
        attempts: EMAIL_MAX_ATTEMPTS,
        failureCode:
          failure === "network"
            ? "email_network_error"
            : `email_http_${failure}`,
      });
      expect(JSON.stringify(await rows(w))).not.toContain(SECRET);
    },
  );

  it.each([422, 401, 409, 302])(
    "fails HTTP %s immediately and never sends after failure",
    async (status) => {
      network.mockImplementation(async () => new Response(null, { status }));
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      await drain(w);
      await w.t.action(internal.notificationEmails.send, {
        notificationId: row._id,
        attempt: 1,
      });
      expect(network).toHaveBeenCalledTimes(1);
      expect(await email(w, row)).toMatchObject({
        status: "failed",
        attempts: 1,
        failureCode: `email_http_${status}`,
      });
    },
  );

  it("stops duplicate actions after success or an already claimed attempt", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: 7,
    });
    expect(network).not.toHaveBeenCalled();
    await drain(w);
    await w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: 0,
    });
    expect(network).toHaveBeenCalledTimes(1);
    expect(await email(w, row)).toMatchObject({ status: "sent", attempts: 1 });
  });

  it("never starts outside the total window or above the attempt bound", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.setSystemTime(NOW + EMAIL_RETRY_WINDOW_MS);
    await drain(w);
    expect(network).not.toHaveBeenCalled();
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      attempts: 0,
      failureCode: "email_retry_exhausted",
    });
    await w.t.run((ctx) =>
      ctx.db.patch(row._id, {
        email: {
          status: "pending",
          attempts: EMAIL_MAX_ATTEMPTS,
          queuedAt: Date.now(),
          updatedAt: Date.now(),
        },
      }),
    );
    await w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: EMAIL_MAX_ATTEMPTS,
    });
    expect(network).not.toHaveBeenCalled();
    expect((await email(w, row))?.status).toBe("failed");
  });

  it("does not schedule a retry that would cross the total window", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.setSystemTime(NOW + EMAIL_RETRY_WINDOW_MS - 1);
    network.mockImplementation(async () => new Response(null, { status: 500 }));
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    expect((await email(w, row))?.status).toBe("failed");
  });

  it.each([{}, { id: 1 }, null])(
    "records an invalid success response %s without storing its body",
    async (body) => {
      network.mockImplementation(async () => Response.json(body));
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      await drain(w);
      expect(network).toHaveBeenCalledTimes(1);
      expect(await email(w, row)).toMatchObject({
        status: "failed",
        failureCode: "email_invalid_response",
      });
    },
  );

  it("ignores a stale result after another attempt or final state", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await w.t.mutation(internal.notificationEmails.finishAttempt, {
      notificationId: row._id,
      attempt: 9,
      outcome: "failed",
    });
    expect((await email(w, row))?.status).toBe("pending");
    await drain(w);
    await w.t.mutation(internal.notificationEmails.finishAttempt, {
      notificationId: row._id,
      attempt: 1,
      outcome: "failed",
    });
    expect((await email(w, row))?.status).toBe("sent");
  });

  it("does not expose a sendable recipient for absent or final e-mail state", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await drain(w);
    expect(
      await w.t.query(internal.notificationEmails.pending, {
        notificationId: row._id,
      }),
    ).toBeNull();
    await w.t.run((ctx) => ctx.db.delete(row._id));
    expect(
      await w.t.query(internal.notificationEmails.pending, {
        notificationId: row._id,
      }),
    ).toBeNull();
  });

  it("C2-d: marks a missing machine as deleted so the page cannot link to its absent record", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await w.t.run((ctx) => ctx.db.delete(w.machine));
    expect(
      (await as(w.t, "manager").query(api.notifications.list, {}))[0],
    ).toMatchObject({ machineName: "-", machineDeleted: true });
    await drain(w);
    expect(network).not.toHaveBeenCalled();
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      failureCode: "email_recipient_gone",
    });
  });
});

describe("deletion, expiry and availability (T02-e, T02-f, T02-g)", () => {
  it("deletes delivery state with the user and skips the scheduled send", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await as(w.t, "orgAdmin").mutation(api.users.deleteUser, {
      userId: w.manager,
    });
    expect(await email(w, row)).toBeUndefined();
    await drain(w);
    expect(network).not.toHaveBeenCalled();
  });

  it("purges delivery state with the alert after 90 days and skips its send", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.setSystemTime(NOW + NOTIFICATION_RETENTION_MS + 1);
    expect(await w.t.mutation(internal.notifications.purge, {})).toEqual({
      deleted: 2,
    });
    await drain(w);
    expect(await rows(w)).toEqual([]);
    expect(await email(w, row)).toBeUndefined();
    expect(network).not.toHaveBeenCalled();
  });

  it.each(["deleted", "clerkDeleted", "emptyEmail"])(
    "rechecks the recipient just before sending (%s)",
    async (change) => {
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      await w.t.run(async (ctx) => {
        if (change === "deleted") await ctx.db.delete(w.manager);
        else
          await ctx.db.patch(
            w.manager,
            change === "clerkDeleted"
              ? { clerkDeletedAt: NOW }
              : { email: " " },
          );
      });
      await drain(w);
      expect(network).not.toHaveBeenCalled();
      expect(await email(w, row)).toMatchObject({
        status: "failed",
        failureCode: "email_recipient_gone",
      });
    },
  );

  it.each(["RESEND_API_KEY", "NOTIFICATION_EMAIL_FROM", "SITE_URL"])(
    "queues nothing and sends nothing without %s",
    async (variable) => {
      vi.stubEnv(variable, "");
      const w = await seedLaunchWorld(modules);
      await queue(w);
      await drain(w);
      expect((await rows(w)).every((row) => row.email === undefined)).toBe(
        true,
      );
      expect(network).not.toHaveBeenCalled();
      expect(
        (await as(w.t, "manager").query(api.notifications.getSettings, {}))
          ?.emailAvailable,
      ).toBe(false);
    },
  );

  it("C2-e: fails queued sends when configuration is removed and never restarts them when restored", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.stubEnv("RESEND_API_KEY", "");
    await drain(w);
    expect(network).not.toHaveBeenCalled();
    expect((await email(w, row))?.failureCode).toBe("email_unavailable");
    vi.stubEnv("RESEND_API_KEY", SECRET);
    await w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: 1,
    });
    expect(network).not.toHaveBeenCalled();
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      attempts: 1,
      failureCode: "email_unavailable",
    });
  });
});

describe("send-time access recheck (C2-a)", () => {
  // The four R1 probe cases now expect no REST call; role and stale data cases extend them.
  it.each([
    "unlinked",
    "membershipInactive",
    "roleRemoved",
    "organizationDeleted",
    "organizationGone",
    "machineDeleted",
    "machineMoved",
    "foreignLink",
  ] as const)("C2-a: rejects a queued recipient after %s", async (change) => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await w.t.run(async (ctx) => {
      switch (change) {
        case "unlinked":
        case "foreignLink": {
          const links = await ctx.db
            .query("machine_gestionnaires")
            .withIndex("by_machine_and_gestionnaire", (q) =>
              q.eq("machineId", w.machine).eq("gestionnaireId", w.manager),
            )
            .collect();
          for (const link of links) {
            if (change === "unlinked") await ctx.db.delete(link._id);
            else await ctx.db.patch(link._id, { organizationId: w.orgB });
          }
          break;
        }
        case "membershipInactive":
        case "roleRemoved": {
          const membership = await ctx.db
            .query("memberships")
            .withIndex("by_user_and_organization", (q) =>
              q.eq("userId", w.manager).eq("organizationId", w.orgA),
            )
            .unique();
          if (!membership) throw new Error("Missing membership");
          await ctx.db.patch(
            membership._id,
            change === "roleRemoved" ? { role: "user" } : { active: false },
          );
          break;
        }
        case "organizationDeleted":
          await ctx.db.patch(w.orgA, { deletedAt: NOW });
          break;
        case "organizationGone":
          await ctx.db.delete(w.orgA);
          break;
        case "machineDeleted":
          await ctx.db.patch(w.machine, { isDeleted: true });
          break;
        case "machineMoved":
          await ctx.db.patch(w.machine, { organizationId: w.orgB });
          break;
      }
    });
    if (change === "unlinked") {
      expect(
        await as(w.t, "manager").query(api.notifications.list, {}),
      ).toEqual([]);
    }
    await drain(w);
    expect(network).not.toHaveBeenCalled();
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      attempts: 1,
      failureCode: "email_recipient_gone",
    });
  });

  it("C2-a: rechecks access again before a retry", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    network.mockImplementationOnce(
      async () => new Response(null, { status: 500 }),
    );
    await w.t.action(internal.notificationEmails.send, {
      notificationId: row._id,
      attempt: 0,
    });
    await w.t.run((ctx) => ctx.db.patch(w.orgA, { deletedAt: NOW }));
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      attempts: 2,
      failureCode: "email_recipient_gone",
    });
  });
});

describe("idempotency conflicts (C2-b)", () => {
  it("C2-b: retries concurrent_idempotent_requests with the same key and the 429 delay", async () => {
    const times: number[] = [];
    network.mockImplementation(async () => {
      times.push(Date.now());
      return times.length === 1
        ? Response.json(
            { name: "concurrent_idempotent_requests", message: SECRET },
            { status: 409 },
          )
        : Response.json({ id: "email_synthetic" });
    });
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await drain(w);
    expect(times).toEqual([NOW, NOW + EMAIL_RETRY_DELAYS_MS[0]]);
    expect(
      network.mock.calls.map(([, init]) =>
        new Headers(init?.headers).get("Idempotency-Key"),
      ),
    ).toEqual([`notification:${row._id}`, `notification:${row._id}`]);
    expect(await email(w, row)).toMatchObject({ status: "sent", attempts: 2 });
    expect(JSON.stringify(await rows(w))).not.toContain(SECRET);
  });

  it("C2-b: records invalid_idempotent_request as a final email_idempotency_conflict", async () => {
    const logs = ["log", "info", "warn", "error", "debug"] as const;
    const spies = logs.map((method) =>
      vi.spyOn(console, method).mockImplementation(() => {}),
    );
    network.mockImplementation(async () =>
      Response.json(
        { name: "invalid_idempotent_request", message: SECRET },
        { status: 409 },
      ),
    );
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      attempts: 1,
      failureCode: "email_idempotency_conflict",
    });
    expect(JSON.stringify(await rows(w))).not.toContain(SECRET);
    expect(JSON.stringify(spies.map((spy) => spy.mock.calls))).not.toContain(
      SECRET,
    );
  });

  it.each([null, "unrecognised", {}, { name: "unknown" }, { name: 409 }])(
    "C2-b: keeps an unknown 409 body %j final with email_http_409",
    async (body) => {
      network.mockImplementation(async () =>
        Response.json(body, { status: 409 }),
      );
      const w = await seedLaunchWorld(modules);
      const row = await queue(w);
      await drain(w);
      expect(network).toHaveBeenCalledTimes(1);
      expect(await email(w, row)).toMatchObject({
        status: "failed",
        failureCode: "email_http_409",
      });
    },
  );

  it("C2-b: keeps an unreadable 409 body final with email_http_409", async () => {
    network.mockImplementation(
      async () => new Response("invalid JSON", { status: 409 }),
    );
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    expect(await email(w, row)).toMatchObject({
      status: "failed",
      failureCode: "email_http_409",
    });
  });
});

describe("abandoned pending e-mails (C2-c)", () => {
  it("C2-c: daily purge closes only stale pending e-mails without extending alert retention", async () => {
    const w = await seedLaunchWorld(modules);
    const cutoff = NOW - EMAIL_ABANDONED_AFTER_MS;
    const inserted = await w.t.run(async (ctx) => {
      const ids = [];
      for (const email of [
        { status: "pending", updatedAt: cutoff - 1 },
        { status: "pending", updatedAt: cutoff },
        { status: "pending", updatedAt: NOW },
        { status: "sent", updatedAt: cutoff - 1 },
        { status: "failed", updatedAt: cutoff - 1 },
      ] as const) {
        ids.push(
          await ctx.db.insert("notifications", {
            organizationId: w.orgA,
            userId: w.manager,
            machineId: w.machine,
            type: "drive_fault",
            count: 1,
            read: false,
            createdAt: cutoff,
            updatedAt: cutoff,
            email: { ...email, attempts: 1, queuedAt: cutoff - 1 },
          }),
        );
      }
      return ids;
    });
    const before = await rows(w);
    expect(await w.t.mutation(internal.notifications.purge, {})).toEqual({
      deleted: 0,
    });
    await drain(w);
    const after = await rows(w);
    expect(after[0]).toEqual({
      ...before[0],
      email: {
        ...before[0].email,
        status: "failed",
        updatedAt: NOW,
        failureCode: "email_abandoned",
      },
    });
    expect(after.slice(1)).toEqual(before.slice(1));
    expect(after.map((row) => row._id)).toEqual(inserted);
    expect(after.every((row) => row.updatedAt === cutoff)).toBe(true);
    expect(EMAIL_ABANDONED_AFTER_MS).toBeGreaterThan(
      Math.max(...EMAIL_RETRY_DELAYS_MS),
    );
    expect(network).not.toHaveBeenCalled();
  });

  it("C2-c: closes at most 500 e-mails per transaction and schedules the remaining batches", async () => {
    const w = await seedLaunchWorld(modules);
    const old = NOW - EMAIL_ABANDONED_AFTER_MS - 1;
    await w.t.run(async (ctx) => {
      for (let i = 0; i < EMAIL_ABANDONED_BATCH_SIZE * 2 + 11; i++) {
        await ctx.db.insert("notifications", {
          organizationId: w.orgA,
          userId: w.manager,
          machineId: w.machine,
          type: "drive_fault",
          count: 1,
          read: false,
          createdAt: old,
          updatedAt: old,
          email: {
            status: "pending",
            attempts: 0,
            queuedAt: old,
            updatedAt: old,
          },
        });
      }
    });
    expect(
      await w.t.mutation(internal.notificationEmails.abandonPending, {}),
    ).toEqual({ failed: 500 });
    const first = await rows(w);
    expect(first.filter((row) => row.email?.status === "failed")).toHaveLength(
      500,
    );
    expect(first.filter((row) => row.email?.status === "pending")).toHaveLength(
      511,
    );
    await drain(w);
    const final = await rows(w);
    expect(final).toHaveLength(1011);
    expect(
      final.every(
        (row) =>
          row.email?.status === "failed" &&
          row.email.failureCode === "email_abandoned",
      ),
    ).toBe(true);
    expect(final.every((row) => row.updatedAt === old)).toBe(true);
    const scheduled = await w.t.run((ctx) =>
      ctx.db.system.query("_scheduled_functions").collect(),
    );
    expect(scheduled).toHaveLength(2);
    expect(
      scheduled.every(
        (job) => job.name === "notificationEmails:abandonPending",
      ),
    ).toBe(true);
  });
});
