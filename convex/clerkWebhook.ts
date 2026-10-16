import {
  httpAction,
  internalMutation,
  internalQuery,
} from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import type { MutationCtx } from "./_generated/server";
import { ConvexError, v } from "convex/values";
import {
  anheartClerkOrganizationId,
  findUnlinkedDefaultOrganization,
  organizationRoleOf,
} from "./lib/auth";
import { normalizedEmail } from "./lib/patientInvitation";
import { audit, SYSTEM_ACTOR } from "./lib/audit";
import {
  clerkEventValidator,
  parseClerkEvent,
  readWebhookBody,
  verifyClerkSignature,
  verifiedClerkEmails,
  type ClerkEvent,
} from "./lib/clerkWebhook";

function stale(
  row: { clerkUpdatedAt?: number; clerkEventAt?: number },
  event: ClerkEvent,
): boolean {
  return (
    row.clerkUpdatedAt !== undefined &&
    (event.updatedAt < row.clerkUpdatedAt ||
      (event.updatedAt === row.clerkUpdatedAt &&
        event.occurredAt <= (row.clerkEventAt ?? 0)))
  );
}

async function organizationFor(
  ctx: MutationCtx,
  event: Exclude<ClerkEvent, { kind: "userDeleted" }>,
): Promise<Doc<"organizations">> {
  const input = event.organization;
  let held = await ctx.db
    .query("organizations")
    .withIndex("by_clerk_org_id", (q) => q.eq("clerkOrgId", input.id))
    .unique();
  if (!held && input.id === anheartClerkOrganizationId())
    held = await findUnlinkedDefaultOrganization(ctx);
  if (held) return held;
  const id = await ctx.db.insert("organizations", {
    clerkOrgId: input.id,
    name: input.name,
    slug: input.slug,
    createdAt: input.createdAt,
    ...(event.kind === "membership"
      ? { clerkUpdatedAt: input.updatedAt, clerkEventAt: event.occurredAt }
      : {}),
    settings: { requirePrescription: false, language: "fr" },
  });
  const created = await ctx.db.get(id);
  if (!created) throw new Error("Organization insert did not persist");
  return created;
}

export const applyEvent = internalMutation({
  args: { eventId: v.string(), event: clerkEventValidator },
  returns: v.null(),
  handler: async (ctx, { eventId, event }) => {
    if (
      await ctx.db
        .query("clerk_webhook_events")
        .withIndex("by_event_id", (q) => q.eq("eventId", eventId))
        .unique()
    )
      return null;
    if (event.kind === "userDeleted") {
      const user = await ctx.db
        .query("users")
        .withIndex("by_clerk_id", (q) => q.eq("clerkId", event.userId))
        .unique();
      if (user) {
        await ctx.db.patch(user._id, { clerkDeletedAt: event.occurredAt });
        const memberships = await ctx.db
          .query("memberships")
          .withIndex("by_user", (q) => q.eq("userId", user._id))
          .collect();
        for (const row of memberships) {
          await ctx.db.patch(row._id, { active: false });
          if (row.active) {
            await audit(ctx, {
              actor: SYSTEM_ACTOR,
              source: "webhook",
              organizationId: row.organizationId,
              action: "account.deleted_in_clerk",
              targetId: user._id,
            });
          }
        }
      } else {
        await ctx.db.insert("users", {
          clerkId: event.userId,
          clerkDeletedAt: event.occurredAt,
          firstName: "",
          lastName: "",
          email: "",
          role: "user",
          language: "fr",
          createdAt: event.occurredAt,
        });
      }
      await ctx.db.insert("clerk_webhook_events", {
        eventId,
        receivedAt: Date.now(),
      });
      return null;
    }
    const organization = await organizationFor(ctx, event);
    if (event.kind === "organization") {
      if (organization.deletedAt === undefined && !stale(organization, event)) {
        await ctx.db.patch(organization._id, {
          clerkOrgId: event.organization.id,
          ...(event.deleted
            ? { deletedAt: event.occurredAt }
            : { name: event.organization.name, slug: event.organization.slug }),
          clerkUpdatedAt: event.updatedAt,
          clerkEventAt: event.occurredAt,
        });
        if (event.deleted) {
          const memberships = await ctx.db
            .query("memberships")
            .withIndex("by_organization", (q) =>
              q.eq("organizationId", organization._id),
            )
            .collect();
          for (const row of memberships)
            await ctx.db.patch(row._id, { active: false });
          await audit(ctx, {
            actor: SYSTEM_ACTOR,
            source: "webhook",
            organizationId: organization._id,
            action: "organization.deleted",
            targetId: organization._id,
            details: { count: memberships.filter((row) => row.active).length },
          });
        }
      }
    } else if (organization.deletedAt === undefined) {
      if (organization.clerkOrgId === undefined)
        await ctx.db.patch(organization._id, {
          clerkOrgId: event.organization.id,
        });
      let user = await ctx.db
        .query("users")
        .withIndex("by_clerk_id", (q) => q.eq("clerkId", event.userId))
        .unique();
      const verifiedEmails = (event.verifiedEmails ?? []).map(normalizedEmail);
      const verifiedEmailSet = new Set(verifiedEmails);
      const invitations = await ctx.db
        .query("patient_invitations")
        .withIndex("by_organization_and_email", (q) =>
          q.eq("organizationId", organization._id),
        )
        .collect();
      const invitationsForUser = invitations.filter((row) =>
        verifiedEmailSet.has(row.email),
      );
      if (!event.deleted && user?.clerkDeletedAt === undefined) {
        const candidates = (await ctx.db.query("users").collect()).filter(
          (row) =>
            row.clerkId === "" &&
            verifiedEmailSet.has(normalizedEmail(row.email)) &&
            (row.organizationId === organization._id ||
              invitationsForUser.some(
                (invitation) => invitation.userId === row._id,
              )),
        );
        if (candidates.length > 1 || (user && candidates.length > 0))
          throw new ConvexError("profile_reconciliation_required");
        if (!user && candidates[0]) {
          user = candidates[0];
          await ctx.db.patch(user._id, { clerkId: event.userId });
        }
        if (
          invitationsForUser.some(
            (invitation) =>
              user &&
              invitation.userId !== undefined &&
              invitation.userId !== user._id,
          )
        )
          throw new ConvexError("profile_reconciliation_required");
      }
      if (!user) {
        if (
          invitationsForUser.some(
            (invitation) => invitation.userId === undefined,
          )
        )
          throw new ConvexError("profile_reconciliation_required");
        const id = await ctx.db.insert("users", {
          clerkId: event.userId,
          firstName: event.firstName,
          lastName: event.lastName,
          email: verifiedEmails.includes(normalizedEmail(event.email))
            ? normalizedEmail(event.email)
            : (verifiedEmails[0] ?? ""),
          role: event.role,
          organizationId: organization._id,
          language: "fr",
          createdAt: event.occurredAt,
        });
        user = await ctx.db.get(id);
      }
      if (!user) throw new Error("User insert did not persist");
      if (
        !event.deleted &&
        invitationsForUser.some(
          (invitation) =>
            invitation.userId !== undefined && invitation.userId !== user._id,
        )
      )
        throw new ConvexError("profile_reconciliation_required");
      const row = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", user._id).eq("organizationId", organization._id),
        )
        .unique();
      const values = {
        clerkMembershipId: event.membershipId,
        role: event.role,
        active: !event.deleted && user.clerkDeletedAt === undefined,
        clerkUpdatedAt: event.updatedAt,
        clerkEventAt: event.occurredAt,
      };
      if (!row) {
        await ctx.db.insert("memberships", {
          userId: user._id,
          organizationId: organization._id,
          ...values,
        });
        await audit(ctx, {
          actor: SYSTEM_ACTOR,
          source: "webhook",
          organizationId: organization._id,
          action: "membership.created",
          targetId: user._id,
          details: { role: event.role },
        });
      } else if (
        !stale(row, event) &&
        (row.active ||
          row.clerkMembershipId !== event.membershipId ||
          event.deleted)
      ) {
        await ctx.db.patch(row._id, values);
        if (row.role !== values.role) {
          await audit(ctx, {
            actor: SYSTEM_ACTOR,
            source: "webhook",
            organizationId: row.organizationId,
            action: "membership.role_changed",
            targetId: user._id,
            details: { role: values.role, previousRole: row.role },
          });
        }
        if (row.active && !values.active) {
          await audit(ctx, {
            actor: SYSTEM_ACTOR,
            source: "webhook",
            organizationId: row.organizationId,
            action: "membership.deactivated",
            targetId: user._id,
          });
        }
      }
      const active = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", user._id).eq("organizationId", organization._id),
        )
        .unique();
      if (
        !event.deleted &&
        active?.active &&
        active.clerkMembershipId === event.membershipId
      ) {
        for (const invitation of invitationsForUser) {
          if (
            (invitation.userId !== undefined &&
              invitation.userId !== user._id) ||
            invitation.status === "accepted"
          )
            continue;
          await ctx.db.patch(invitation._id, {
            userId: user._id,
            status: "accepted",
            updatedAt: Date.now(),
          });
          for (const gestionnaireId of invitation.gestionnaireIds) {
            // Invitation acceptance links are covered by user.invited.
            if (event.role !== "user") continue;
            if (
              (await organizationRoleOf(
                ctx,
                gestionnaireId,
                organization._id,
              )) !== "gestionnaire"
            )
              continue;
            const links = await ctx.db
              .query("user_gestionnaires")
              .withIndex("by_user_and_gestionnaire", (q) =>
                q.eq("userId", user._id).eq("gestionnaireId", gestionnaireId),
              )
              .collect();
            if (!links.some((link) => link.organizationId === organization._id))
              await ctx.db.insert("user_gestionnaires", {
                userId: user._id,
                gestionnaireId,
                organizationId: organization._id,
                createdAt: Date.now(),
                createdBy: invitation.requestedBy,
              });
          }
        }
      }
    }
    await ctx.db.insert("clerk_webhook_events", {
      eventId,
      receivedAt: Date.now(),
    });
    return null;
  },
});

export const wasProcessed = internalQuery({
  args: { eventId: v.string() },
  returns: v.boolean(),
  handler: async (ctx, { eventId }) =>
    (await ctx.db
      .query("clerk_webhook_events")
      .withIndex("by_event_id", (q) => q.eq("eventId", eventId))
      .unique()) !== null,
});

export const receive = httpAction(async (ctx, request) => {
  const secret = process.env.CLERK_WEBHOOK_SIGNING_SECRET;
  if (!secret) return new Response("Webhook unavailable", { status: 503 });
  const body = await readWebhookBody(request);
  if (body === null) return new Response("Invalid payload", { status: 400 });
  const eventId = await verifyClerkSignature(
    request.headers,
    body,
    secret,
    Date.now(),
  );
  if (eventId === null)
    return new Response("Invalid signature", { status: 401 });
  let value: unknown;
  try {
    value = JSON.parse(body);
  } catch {
    return new Response("Invalid payload", { status: 400 });
  }
  const event = parseClerkEvent(value);
  if (event === null) return new Response("Invalid payload", { status: 400 });
  if (await ctx.runQuery(internal.clerkWebhook.wasProcessed, { eventId }))
    return new Response(null, { status: 204 });
  if (event !== "ignored" && event.kind === "membership" && !event.deleted) {
    const emails = await verifiedClerkEmails(event.userId);
    if (emails === null || emails.length === 0)
      return new Response("Identity verification unavailable", { status: 503 });
    event.verifiedEmails = emails;
  }
  if (event !== "ignored")
    await ctx.runMutation(internal.clerkWebhook.applyEvent, { eventId, event });
  return new Response(null, { status: 204 });
});
