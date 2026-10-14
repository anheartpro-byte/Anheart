import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, internal } from "./_generated/api";
import { as, modules, NOW, ORG_A_CLERK_ORG, seedWorld } from "./test.setup";
import {
  acceptPatientInvitation,
  mockClerkInvitations,
} from "./patientInvitation.fixtures";
import { verifiedClerkEmails } from "./lib/clerkWebhook";
import { sendPatientInvitation } from "./lib/patientInvitation";

const input = {
  firstName: "Invited",
  lastName: "Patient",
  email: "invited@example.invalid",
};
beforeEach(mockClerkInvitations);
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("patient invitations", () => {
  it("does not reveal ambiguous foreign email matches and resolves only the verified Clerk account", async () => {
    const w = await seedWorld(modules);
    const original = await w.t.run((ctx) => ctx.db.get(w.orgBPatient));
    if (!original) throw new Error("Missing patient");
    const duplicateId = await w.t.run((ctx) =>
      ctx.db.insert("users", {
        clerkId: "legacy_foreign_duplicate",
        email: original.email,
        firstName: "Private",
        lastName: "Legacy",
        organizationId: w.orgB,
        role: "user",
        language: "fr",
        createdAt: NOW,
      }),
    );
    const before = await w.t.run((ctx) => ctx.db.query("users").collect());
    const sent = await as(w.t, "manager").action(api.users.createPatient, {
      ...input,
      email: original.email,
    });
    expect(sent).toEqual({
      invitationId: expect.any(String),
      email: original.email.toLowerCase(),
      status: "sent",
    });
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(
      (await w.t.run((ctx) => ctx.db.get(sent.invitationId)))?.userId,
    ).toBeUndefined();
    expect(await w.t.run((ctx) => ctx.db.query("users").collect())).toEqual(
      before,
    );
    expect(
      await as(w.t, "manager").query(api.users.getUserById, {
        userId: original._id,
      }),
    ).toBeNull();
    await expect(
      acceptPatientInvitation(w, sent.invitationId, "unknown_clerk_account"),
    ).rejects.toThrow("profile_reconciliation_required");
    expect(await w.t.run((ctx) => ctx.db.query("users").collect())).toEqual(
      before,
    );
    expect(
      await acceptPatientInvitation(w, sent.invitationId, original.clerkId),
    ).toBe(original._id);
    expect(await w.t.run((ctx) => ctx.db.get(original._id))).toEqual(original);
    expect((await w.t.run((ctx) => ctx.db.get(duplicateId)))?.clerkId).toBe(
      "legacy_foreign_duplicate",
    );
    expect(
      await w.t.run((ctx) => ctx.db.query("users").collect()),
    ).toHaveLength(before.length);
  });
  it("reuses a foreign account without disclosing it or changing its profile before acceptance", async () => {
    const w = await seedWorld(modules);
    const original = await w.t.run((ctx) => ctx.db.get(w.orgBPatient));
    if (!original) throw new Error("Missing patient");
    const sent = await as(w.t, "manager").action(api.users.createPatient, {
      ...input,
      email: original.email,
    });
    expect(Object.keys(sent).sort()).toEqual([
      "email",
      "invitationId",
      "status",
    ]);
    expect(
      await as(w.t, "manager").query(api.users.getUserById, {
        userId: original._id,
      }),
    ).toBeNull();
    expect(await acceptPatientInvitation(w, sent.invitationId)).toBe(
      original._id,
    );
    expect(await w.t.run((ctx) => ctx.db.get(original._id))).toEqual(original);
    expect(
      (
        await as(w.t, "manager").query(api.users.getUserById, {
          userId: original._id,
        })
      )?._id,
    ).toBe(original._id);
    expect(
      (await w.t.run((ctx) => ctx.db.query("users").collect())).filter(
        (row) => row.clerkId === original.clerkId,
      ),
    ).toHaveLength(1);
  });
  it("sends once, keeps the profile private until acceptance, and links the same record once", async () => {
    const w = await seedWorld(modules);
    const manager = as(w.t, "manager");
    const before = await w.t.run((ctx) => ctx.db.query("users").collect());
    const sent = await manager.action(api.users.createPatient, input);
    const again = await manager.action(api.users.createPatient, input);
    expect(again).toEqual(sent);
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining(ORG_A_CLERK_ORG),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          email_address: input.email,
          role: "org:patient",
        }),
      }),
    );
    const invitation = await w.t.run((ctx) => ctx.db.get(sent.invitationId));
    if (!invitation?.userId) throw new Error("Missing prepared profile");
    expect(
      await manager.query(api.users.getUserById, { userId: invitation.userId }),
    ).toBeNull();
    const userId = await acceptPatientInvitation(w, sent.invitationId);
    await acceptPatientInvitation(w, sent.invitationId);
    expect(userId).toBe(invitation.userId);
    expect(
      (await manager.query(api.users.getUserById, { userId }))?.firstName,
    ).toBe("Invited");
    const rows = await w.t.run((ctx) => ctx.db.query("users").collect());
    expect(rows).toHaveLength(before.length + 1);
    expect(
      rows.filter((row) => row.clerkId === `user_${sent.invitationId}`),
    ).toHaveLength(1);
    expect(
      (await w.t.run((ctx) => ctx.db.get(sent.invitationId)))?.status,
    ).toBe("accepted");
    await expect(
      manager.action(api.users.createPatient, input),
    ).rejects.toThrow("invitation_unavailable");
  });

  it("reports an unconfirmed delivery and retries the original profile", async () => {
    const w = await seedWorld(modules);
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 503 })),
    );
    await expect(
      as(w.t, "manager").action(api.users.createPatient, input),
    ).rejects.toThrow("invitation_failed");
    const failed = await w.t.run((ctx) =>
      ctx.db.query("patient_invitations").unique(),
    );
    expect(failed?.status).toBe("failed");
    mockClerkInvitations();
    const retry = await as(w.t, "manager").action(
      api.users.createPatient,
      input,
    );
    expect(retry.invitationId).toBe(failed?._id);
    expect(
      (await w.t.run((ctx) => ctx.db.query("users").collect())).filter(
        (row) => row.email === input.email,
      ),
    ).toHaveLength(1);
  });

  it("cannot create a second profile during first-login synchronization", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "manager").action(api.users.createPatient, input);
    await expect(
      w.t
        .withIdentity({
          subject: "new_account",
          email: input.email,
          emailVerified: true,
        })
        .mutation(api.users.getOrCreateUser, {}),
    ).rejects.toThrow("profile_pending");
  });

  it("refuses linking when this Clerk account already has a different profile", async () => {
    const w = await seedWorld(modules);
    await as(w.t, "manager").action(api.users.createPatient, input);
    expect(
      await as(w.t, "stranger", {
        email: input.email,
        emailVerified: true,
      }).mutation(api.users.linkPatientToClerk, { email: input.email }),
    ).toBeNull();
    expect(
      (await w.t.run((ctx) => ctx.db.query("users").collect())).filter(
        (row) => row.clerkId === "stranger",
      ),
    ).toHaveLength(1);
  });

  it("does not revive a deleted account through a delayed membership event", async () => {
    const w = await seedWorld(modules);
    const sent = await as(w.t, "manager").action(
      api.users.createPatient,
      input,
    );
    const clerkId = `user_${sent.invitationId}`;
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "deleted_first",
      event: {
        kind: "userDeleted",
        userId: clerkId,
        occurredAt: NOW + 100,
        updatedAt: NOW + 100,
      },
    });
    await expect(acceptPatientInvitation(w, sent.invitationId)).rejects.toThrow(
      "profile_reconciliation_required",
    );
    expect(
      await w.t
        .withIdentity({ subject: clerkId })
        .query(api.users.getCurrentUser, {}),
    ).toBeNull();
    expect(
      (await w.t.run((ctx) => ctx.db.query("memberships").collect())).some(
        (row) => row.clerkMembershipId === `orgmem_${sent.invitationId}`,
      ),
    ).toBe(false);
  });

  it("deactivates every membership when Clerk deletes a linked user", async () => {
    const w = await seedWorld(modules);
    const sent = await as(w.t, "manager").action(
      api.users.createPatient,
      input,
    );
    const id = await acceptPatientInvitation(w, sent.invitationId);
    await w.t.mutation(internal.clerkWebhook.applyEvent, {
      eventId: "deleted_after",
      event: {
        kind: "userDeleted",
        userId: `user_${sent.invitationId}`,
        occurredAt: NOW + 100,
        updatedAt: NOW + 100,
      },
    });
    expect(
      (
        await w.t.run((ctx) =>
          ctx.db
            .query("memberships")
            .withIndex("by_user", (q) => q.eq("userId", id))
            .collect(),
        )
      ).every((row) => !row.active),
    ).toBe(true);
    expect((await w.t.run((ctx) => ctx.db.get(id)))?.clerkDeletedAt).toBe(
      NOW + 100,
    );
  });

  it("rejects malformed names and emails before calling Clerk", async () => {
    const w = await seedWorld(modules);
    for (const args of [
      { ...input, firstName: " " },
      { ...input, email: "not-email" },
    ])
      await expect(
        as(w.t, "manager").action(api.users.createPatient, args),
      ).rejects.toThrow("invalid_invitation");
    expect(fetch).not.toHaveBeenCalled();
  });
});

describe("Clerk response verification", () => {
  it("does not confirm an invitation carrying a different role", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "orginv_admin",
          email_address: input.email,
          status: "pending",
          role: "org:admin",
        }),
      ),
    );
    expect(
      await sendPatientInvitation("synthetic", "org_test", input.email),
    ).toBeNull();
  });
  it("keeps only verified addresses belonging to the requested account", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "user_verified",
          email_addresses: [
            {
              email_address: "verified@example.invalid",
              verification: { status: "verified" },
            },
            {
              email_address: "unverified@example.invalid",
              verification: { status: "unverified" },
            },
            { email_address: "pending@example.invalid", verification: null },
          ],
        }),
      ),
    );
    expect(await verifiedClerkEmails("user_verified")).toEqual([
      "verified@example.invalid",
    ]);
    expect(await verifiedClerkEmails("user_other")).toBeNull();
  });

  it.each(["configuration", "network", "http", "json"])(
    "fails closed on %s errors",
    async (failure) => {
      if (failure === "configuration") vi.stubEnv("CLERK_SECRET_KEY", "");
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => {
          if (failure === "network") throw new Error("offline");
          return new Response("invalid", {
            status: failure === "http" ? 503 : 200,
          });
        }),
      );
      expect(await verifiedClerkEmails("user_verified")).toBeNull();
    },
  );

  it("recovers a pending invitation after a previous provider acknowledgement was lost", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(null, { status: 422 }))
        .mockResolvedValueOnce(
          Response.json({
            data: [
              {
                id: "orginv_held",
                email_address: input.email,
                status: "pending",
                role: "org:patient",
              },
            ],
            total_count: 1,
          }),
        ),
    );
    expect(
      await sendPatientInvitation("synthetic", "org_test", input.email),
    ).toBe("orginv_held");
  });

  it("does not confirm a provider response for a different email", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        Response.json({
          id: "orginv_wrong",
          email_address: "different@example.invalid",
          status: "pending",
          role: "org:patient",
        }),
      ),
    );
    expect(
      await sendPatientInvitation("synthetic", "org_test", input.email),
    ).toBeNull();
  });
});
