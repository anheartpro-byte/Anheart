import { httpAction, internalMutation } from "./_generated/server";
import { internal } from "./_generated/api";
import type { Doc } from "./_generated/dataModel";
import type { MutationCtx } from "./_generated/server";
import { v } from "convex/values";
import {
  anheartClerkOrganizationId,
  findUnlinkedDefaultOrganization,
} from "./lib/auth";
import {
  clerkEventValidator,
  parseClerkEvent,
  readWebhookBody,
  verifyClerkSignature,
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
  event: ClerkEvent,
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
      if (!user) {
        const id = await ctx.db.insert("users", {
          clerkId: event.userId,
          firstName: event.firstName,
          lastName: event.lastName,
          email: event.email,
          role: event.role,
          organizationId: organization._id,
          language: "fr",
          createdAt: event.occurredAt,
        });
        user = await ctx.db.get(id);
      }
      if (!user) throw new Error("User insert did not persist");
      const row = await ctx.db
        .query("memberships")
        .withIndex("by_user_and_organization", (q) =>
          q.eq("userId", user._id).eq("organizationId", organization._id),
        )
        .unique();
      const values = {
        clerkMembershipId: event.membershipId,
        role: event.role,
        active: !event.deleted,
        clerkUpdatedAt: event.updatedAt,
        clerkEventAt: event.occurredAt,
      };
      if (!row) {
        await ctx.db.insert("memberships", {
          userId: user._id,
          organizationId: organization._id,
          ...values,
        });
      } else if (
        !stale(row, event) &&
        (row.active ||
          row.clerkMembershipId !== event.membershipId ||
          event.deleted)
      ) {
        await ctx.db.patch(row._id, values);
      }
    }
    await ctx.db.insert("clerk_webhook_events", {
      eventId,
      receivedAt: Date.now(),
    });
    return null;
  },
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
  if (event !== "ignored")
    await ctx.runMutation(internal.clerkWebhook.applyEvent, { eventId, event });
  return new Response(null, { status: 204 });
});
