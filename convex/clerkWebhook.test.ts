/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "./_generated/api";
import {
  parseClerkEvent,
  verifiedClerkEmails,
  verifyClerkSignature,
} from "./lib/clerkWebhook";
import {
  as,
  backendOf,
  configureAnheartOrganization,
  modules,
  NOW,
  ORG_B_CLERK_ORG,
  seedWorld,
} from "./test.setup";

const secret = `whsec_${btoa("synthetic-webhook-signing-key")}`;
const makeTest = () => backendOf(modules);
type Test = ReturnType<typeof makeTest>;
const org = {
  id: "org_qa_webhook",
  name: "QA Organization",
  slug: "qa-webhook",
  created_at: NOW - 10000,
  updated_at: NOW - 1000,
};
const member = {
  id: "orgmem_qa",
  organization: org,
  role: "org:patient",
  updated_at: NOW - 500,
  public_user_data: {
    user_id: "user_qa",
    first_name: "QA",
    last_name: "Member",
    identifier: "qa@example.invalid",
  },
};
const event = (type: string, data: unknown, timestamp = NOW) => ({
  type,
  data,
  timestamp,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  vi.stubEnv("CLERK_WEBHOOK_SIGNING_SECRET", secret);
  vi.stubEnv("CLERK_SECRET_KEY", "sk_test_synthetic");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      Response.json({
        id: url.split("/").slice(-1)[0],
        email_addresses: [
          {
            email_address: "qa@example.invalid",
            verification: { status: "verified" },
          },
        ],
      }),
    ),
  );
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  configureAnheartOrganization(null);
});

async function signed(
  body: string,
  id: string,
  timestamp = Math.floor(NOW / 1000),
): Promise<Headers> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode("synthetic-webhook-signing-key"),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const signature = new Uint8Array(
    await crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(`${id}.${timestamp}.${body}`),
    ),
  );
  return new Headers({
    "svix-id": id,
    "svix-timestamp": String(timestamp),
    "svix-signature": `v1,${btoa(String.fromCharCode(...signature))}`,
  });
}

async function send(t: Test, value: unknown, id = "msg_qa"): Promise<Response> {
  const body = JSON.stringify(value);
  return t.fetch("/clerk/webhook", {
    method: "POST",
    body,
    headers: await signed(body, id),
  });
}

describe("Clerk webhook signature and persistence", () => {
  it("accepts organizations with slugs disabled in Clerk", async () => {
    const t = makeTest();
    expect((await send(t, event("organization.created", { ...org, slug: null }))).status).toBe(204);
    expect((await t.run((ctx) => ctx.db.query("organizations").unique()))?.slug).toBe(org.id);
  });
  it("retries verification failures without recording or linking the event", async () => {
    const t = makeTest();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "user_qa",
          email_addresses: [
            {
              email_address: "qa@example.invalid",
              verification: { status: "unverified" },
            },
          ],
        }),
      ),
    );
    expect(
      (await send(t, event("organizationMembership.created", member))).status,
    ).toBe(503);
    expect(await t.run((ctx) => ctx.db.query("users").collect())).toEqual([]);
    expect(
      await t.run((ctx) => ctx.db.query("clerk_webhook_events").collect()),
    ).toEqual([]);
  });

  it("acknowledges a duplicate without needing the provider after account deletion", async () => {
    const t = makeTest();
    expect(
      (
        await send(
          t,
          event("organizationMembership.created", member),
          "original",
        )
      ).status,
    ).toBe(204);
    expect(
      (await send(t, event("user.deleted", { id: "user_qa" }), "deletion"))
        .status,
    ).toBe(204);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 404 })),
    );
    expect(
      (
        await send(
          t,
          event("organizationMembership.created", member),
          "original",
        )
      ).status,
    ).toBe(204);
    expect(fetch).not.toHaveBeenCalled();
    expect(
      (await t.run((ctx) => ctx.db.query("users").unique()))?.clerkDeletedAt,
    ).toBe(NOW);
  });
  it("matches the published Svix verification vector", async () => {
    expect(
      await verifyClerkSignature(
        new Headers({
          "svix-id": "msg_loFOjxBNrRLzqYUf",
          "svix-timestamp": "1731705121",
          "svix-signature": "v1,rAvfW3dJ/X/qxhsaXPOyyCGmRKsaKWcsNccKXlIktD0=",
        }),
        '{"event_type":"ping","data":{"success":true}}',
        "whsec_plJ3nmyCDGBKInavdOK15jsl",
        1731705121000,
      ),
    ).toBe("msg_loFOjxBNrRLzqYUf");
  });

  it.each(["signature", "body", "past", "future", "missing"] as const)(
    "refuses %s verification failures before writing",
    async (kind) => {
      const t = makeTest();
      let body = JSON.stringify(event("organization.created", org));
      const headers = await signed(
        body,
        "msg_invalid",
        Math.floor(NOW / 1000) +
          (kind === "past" ? -301 : kind === "future" ? 301 : 0),
      );
      if (kind === "signature") headers.set("svix-signature", "v1,invalid");
      if (kind === "body") body += " ";
      if (kind === "missing") headers.delete("svix-id");
      expect(
        (
          await t.fetch("/clerk/webhook", {
            method: "POST",
            body,
            headers,
          })
        ).status,
      ).toBe(401);
      expect(
        await t.run((ctx) => ctx.db.query("organizations").collect()),
      ).toEqual([]);
      expect(
        await t.run((ctx) => ctx.db.query("clerk_webhook_events").collect()),
      ).toEqual([]);
    },
  );

  it("accepts one valid signature among versioned alternatives", async () => {
    const t = makeTest();
    const body = JSON.stringify(event("organization.created", org));
    const headers = await signed(body, "msg_multiple");
    headers.set(
      "svix-signature",
      `v2,ignored v1,invalid ${headers.get("svix-signature")}`,
    );
    expect(
      (await t.fetch("/clerk/webhook", { method: "POST", body, headers }))
        .status,
    ).toBe(204);
  });

  it("fails closed without configuration and bounds the raw body", async () => {
    const t = makeTest();
    vi.stubEnv("CLERK_WEBHOOK_SIGNING_SECRET", "");
    expect((await send(t, event("organization.created", org))).status).toBe(
      503,
    );
    vi.stubEnv("CLERK_WEBHOOK_SIGNING_SECRET", secret);
    expect(
      (
        await t.fetch("/clerk/webhook", {
          method: "POST",
          body: " ".repeat(1024 * 1024 + 1),
        })
      ).status,
    ).toBe(400);
    expect(
      await t.run((ctx) => ctx.db.query("organizations").collect()),
    ).toEqual([]);
  });

  it("rejects invalid signed payloads and acknowledges unrelated event kinds", async () => {
    const t = makeTest();
    const raw = "not json";
    expect(
      (
        await t.fetch("/clerk/webhook", {
          method: "POST",
          body: raw,
          headers: await signed(raw, "msg_parse"),
        })
      ).status,
    ).toBe(400);
    expect(
      (
        await send(
          t,
          event("organization.created", { id: "org_missing_fields" }),
        )
      ).status,
    ).toBe(400);
    expect(
      (
        await send(
          t,
          event("organizationMembership.created", {
            ...member,
            role: "org:unknown",
          }),
        )
      ).status,
    ).toBe(400);
    expect((await send(t, event("user.updated", {}))).status).toBe(204);
    expect(
      await t.run((ctx) => ctx.db.query("clerk_webhook_events").collect()),
    ).toEqual([]);
  });

  it("upserts an organization and ignores duplicate and stale deliveries", async () => {
    const t = makeTest();
    const newer = event(
      "organization.updated",
      { ...org, name: "New name", updated_at: NOW },
      NOW,
    );
    expect((await send(t, newer, "msg_new")).status).toBe(204);
    expect((await send(t, newer, "msg_new")).status).toBe(204);
    expect(
      (await send(t, event("organization.created", org, NOW - 1000), "msg_old"))
        .status,
    ).toBe(204);
    const rows = await t.run((ctx) => ctx.db.query("organizations").collect());
    expect(rows).toHaveLength(1);
    expect(rows[0]?.name).toBe("New name");
    expect(
      await t.run((ctx) => ctx.db.query("clerk_webhook_events").collect()),
    ).toHaveLength(2);
  });

  it("creates the invited patient's membership from a signed event without manual rows", async () => {
    const t = makeTest();
    expect(
      (await send(t, event("organizationMembership.created", member))).status,
    ).toBe(204);
    const rows = await t.run(async (ctx) => ({
      users: await ctx.db.query("users").collect(),
      memberships: await ctx.db.query("memberships").collect(),
    }));
    expect(rows.users).toHaveLength(1);
    expect(rows.users[0]?.clerkId).toBe("user_qa");
    expect(rows.memberships).toMatchObject([
      {
        userId: rows.users[0]?._id,
        role: "user",
        active: true,
        clerkMembershipId: "orgmem_qa",
      },
    ]);
  });

  it("does not resurrect a deleted organization or its memberships from late events", async () => {
    const t = makeTest();
    await send(
      t,
      event("organizationMembership.created", member),
      "msg_member",
    );
    await send(t, event("organization.deleted", { id: org.id }), "msg_delete");
    await send(
      t,
      event(
        "organization.created",
        { ...org, updated_at: NOW + 1000 },
        NOW + 1000,
      ),
      "msg_late_org",
    );
    await send(
      t,
      event(
        "organizationMembership.updated",
        { ...member, updated_at: NOW + 2000 },
        NOW + 2000,
      ),
      "msg_late_member",
    );
    expect(
      (await t.run((ctx) => ctx.db.query("organizations").unique()))?.deletedAt,
    ).toBe(NOW);
    expect(
      (await t.run((ctx) => ctx.db.query("memberships").unique()))?.active,
    ).toBe(false);
  });

  it("keeps a deletion that arrives before the organization creation", async () => {
    const t = makeTest();
    await send(
      t,
      event("organization.deleted", { id: org.id }),
      "msg_delete_first",
    );
    await send(
      t,
      event("organization.created", org, NOW - 1000),
      "msg_old_create",
    );
    expect(
      (await t.run((ctx) => ctx.db.query("organizations").unique()))?.deletedAt,
    ).toBe(NOW);
  });

  it("preserves membership removal across stale updates and a stale token, while allowing a new invitation", async () => {
    const t = makeTest();
    await send(
      t,
      event("organizationMembership.created", member),
      "msg_create",
    );
    await send(
      t,
      event("organizationMembership.deleted", member),
      "msg_delete",
    );
    await send(
      t,
      event("organizationMembership.updated", member, NOW - 100),
      "msg_stale",
    );
    await t
      .withIdentity({
        subject: "user_qa",
        org_id: org.id,
        org_role: "org:patient",
      })
      .mutation(api.users.getOrCreateUser, {});
    expect(
      (await t.run((ctx) => ctx.db.query("memberships").unique()))?.active,
    ).toBe(false);
    await send(
      t,
      event(
        "organizationMembership.created",
        { ...member, id: "orgmem_reinvited", updated_at: NOW + 1000 },
        NOW + 1000,
      ),
      "msg_rejoin",
    );
    expect(
      (await t.run((ctx) => ctx.db.query("memberships").unique()))?.active,
    ).toBe(true);
  });

  it("links the configured Anheart identifier to the default organization", async () => {
    const t = makeTest();
    const id = await t.run((ctx) =>
      ctx.db.insert("organizations", {
        name: "Default",
        slug: "anheart",
        createdAt: NOW,
        settings: { requirePrescription: false, language: "fr" },
      }),
    );
    vi.stubEnv("ANHEART_ORG_ID", org.id);
    await send(t, event("organization.created", org));
    expect(
      await t.run((ctx) => ctx.db.query("organizations").collect()),
    ).toHaveLength(1);
    expect((await t.run((ctx) => ctx.db.get(id)))?.clerkOrgId).toBe(org.id);
  });
});

describe("membership boundaries", () => {
  it("preserves the account owner's name and language edits across memberships", async () => {
    const w = await seedWorld(modules);
    await w.t.run((ctx) =>
      ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      }),
    );
    await as(w.t, "patient").mutation(api.users.updateUserProfile, {
      firstName: "Owner",
      language: "en",
    });
    expect(await w.t.run((ctx) => ctx.db.get(w.patient))).toMatchObject({
      firstName: "Owner",
      language: "en",
    });
  });

  it("keeps signed membership ordering and does not overwrite it from a token", async () => {
    const t = makeTest();
    await send(
      t,
      event(
        "organizationMembership.updated",
        { ...member, role: "org:gestionnaire", updated_at: NOW },
        NOW,
      ),
      "msg_new_role",
    );
    await send(
      t,
      event("organizationMembership.created", member, NOW - 1000),
      "msg_old_role",
    );
    await t
      .withIdentity({
        subject: "user_qa",
        org_id: org.id,
        org_role: "org:patient",
      })
      .mutation(api.users.getOrCreateUser, {});
    expect(
      (await t.run((ctx) => ctx.db.query("memberships").unique()))?.role,
    ).toBe("gestionnaire");
  });

  it("uses event timestamps to break equal object timestamps", async () => {
    const t = makeTest();
    await send(
      t,
      event("organization.updated", { ...org, name: "Latest" }, NOW),
      "msg_latest",
    );
    await send(t, event("organization.updated", org, NOW - 1), "msg_tie_old");
    expect(
      (await t.run((ctx) => ctx.db.query("organizations").unique()))?.name,
    ).toBe("Latest");
  });

  it("uses a verified Clerk email when the public identifier is a username", async () => {
    const t = makeTest();
    await send(
      t,
      event("organizationMembership.created", {
        ...member,
        public_user_data: {
          ...member.public_user_data,
          identifier: "qa-user-name",
        },
      }),
    );
    expect((await t.run((ctx) => ctx.db.query("users").unique()))?.email).toBe(
      "qa@example.invalid",
    );
  });

  it("refuses a deleted organization in a still-valid identity", async () => {
    const t = makeTest();
    await send(
      t,
      event("organizationMembership.created", member),
      "msg_create",
    );
    await send(t, event("organization.deleted", { id: org.id }), "msg_remove");
    const current = await t
      .withIdentity({
        subject: "user_qa",
        org_id: org.id,
        org_role: "org:patient",
      })
      .query(api.users.getCurrentUser, {});
    expect(current?.organization).toBeNull();
  });

  it("reserves shared profile changes to Anheart and permits an own-session stop from another organization", async () => {
    const w = await seedWorld(modules);
    const sessionId = await w.t.run(async (ctx) => {
      await ctx.db.insert("memberships", {
        userId: w.patient,
        organizationId: w.orgB,
        role: "user",
        active: true,
      });
      return ctx.db.insert("sessions", {
        organizationId: w.orgA,
        machineId: w.machine,
        userId: w.patient,
        status: "active",
        startedAt: NOW,
        channels: ["ECG"],
        kind: "auto",
      });
    });
    await expect(
      as(w.t, "manager").mutation(api.users.updatePatient, {
        userId: w.patient,
        firstName: "Changed",
      }),
    ).rejects.toThrow(
      "shared_profile_admin_only",
    );
    await expect(
      as(w.t, "manager").mutation(api.training.setUserPhysiology, {
        userId: w.patient,
        hrMax: 180,
      }),
    ).rejects.toThrow(
      "shared_profile_admin_only",
    );
    await as(w.t, "admin").mutation(api.training.setUserPhysiology, {
      userId: w.patient,
      hrMax: 180,
    });
    await w.t
      .withIdentity({
        subject: "patient",
        org_id: ORG_B_CLERK_ORG,
        org_role: "org:patient",
      })
      .mutation(api.training.requestStop, { sessionId });
    expect(
      (await w.t.run((ctx) => ctx.db.get(sessionId)))?.stopRequestedAt,
    ).toBe(NOW);
  });

  it("filters inactive members out of launch rights", async () => {
    const w = await seedWorld(modules);
    await w.t.run(async (ctx) => {
      const row = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", w.patient).eq("organizationId", w.orgA),
        )
        .unique();
      if (!row) throw new Error("Missing fixture membership");
      await ctx.db.patch(row._id, { active: false });
    });
    expect(
      await as(w.t, "admin").query(api.training.listLaunchRights, {
        machineId: w.machine,
      }),
    ).toEqual([]);
  });

  it("uses the membership role rather than the account mirror during transition", async () => {
    const w = await seedWorld(modules);
    configureAnheartOrganization(null);
    await w.t.run((ctx) => ctx.db.patch(w.patient, { role: "admin" }));
    const user = await w.t
      .withIdentity({ subject: "patient" })
      .query(api.users.getCurrentUser, {});
    expect(user?.role).toBe("user");
  });
});

describe("ANH-249 the schemas of a Clerk payload, under zod 4.6", () => {
  it("reads an envelope only with a type, a valid timestamp and its data", () => {
    expect(parseClerkEvent(event("user.updated", {}))).toBe("ignored");
    for (const value of [
      { type: "user.updated", timestamp: NOW },
      event("user.updated", {}, -1),
      { ...event("user.updated", {}), type: 1 },
      "text",
    ])
      expect(parseClerkEvent(value)).toBeNull();
  });

  it("reads a deletion only with the identifier of what is deleted", () => {
    expect(parseClerkEvent(event("user.deleted", { id: "user_qa" }))).toEqual({
      kind: "userDeleted",
      userId: "user_qa",
      occurredAt: NOW,
      updatedAt: NOW,
    });
    expect(
      parseClerkEvent(event("organization.deleted", { id: org.id })),
    ).toMatchObject({ kind: "organization", deleted: true, organization: { id: org.id } });
    for (const type of ["user.deleted", "organization.deleted"])
      for (const data of [{}, { id: "" }, { id: "x".repeat(257) }])
        expect(parseClerkEvent(event(type, data))).toBeNull();
  });

  it("keeps the length limit of an organization name", () => {
    const named = (name: string) =>
      parseClerkEvent(event("organization.created", { ...org, name }));
    expect(named("x".repeat(1024))).toMatchObject({ kind: "organization" });
    expect(named("x".repeat(1025))).toBeNull();
  });

  it("takes the public identifier as the email only when it is an address", () => {
    const identified = (identifier: string) =>
      parseClerkEvent(
        event("organizationMembership.created", {
          ...member,
          public_user_data: { ...member.public_user_data, identifier },
        }),
      );
    expect(identified("qa@example.invalid")).toMatchObject({
      email: "qa@example.invalid",
    });
    expect(identified("qa-user-name")).toMatchObject({ email: "" });
  });

  it("refuses a Clerk account whose addresses are not email addresses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "user_qa",
          email_addresses: [
            {
              email_address: "not-an-address",
              verification: { status: "verified" },
            },
          ],
        }),
      ),
    );
    expect(await verifiedClerkEmails("user_qa")).toBeNull();
  });
});
