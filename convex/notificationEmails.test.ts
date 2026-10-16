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

  it("uses the flattened fallback name for a machine whose row disappeared", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    await w.t.run((ctx) => ctx.db.delete(w.machine));
    expect(
      (await as(w.t, "manager").query(api.notifications.list, {}))[0]
        .machineName,
    ).toBe("-");
    await drain(w);
    expect(network).toHaveBeenCalledTimes(1);
    expect(
      JSON.parse(String(network.mock.calls[0][1]?.body)).subject,
    ).toContain("-");
    expect((await email(w, row))?.status).toBe("sent");
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
      expect((await email(w, row))?.status).toBe("failed");
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

  it("reads the key at sending time and stops when configuration was removed", async () => {
    const w = await seedLaunchWorld(modules);
    const row = await queue(w);
    vi.stubEnv("RESEND_API_KEY", "");
    await drain(w);
    expect(network).not.toHaveBeenCalled();
    expect((await email(w, row))?.failureCode).toBe("email_unavailable");
  });
});
