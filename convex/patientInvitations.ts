import { ConvexError, v } from "convex/values";
import { internalMutation } from "./_generated/server";
import { audit, userActor } from "./lib/audit";
import type { Id } from "./_generated/dataModel";
import {
  canAccessUser,
  organizationRoleOf,
  requireRole,
  STAFF_ROLES,
} from "./lib/auth";
import {
  invitationInput,
  normalizedEmail,
  patientInvitationArgs,
} from "./lib/patientInvitation";

export const prepare = internalMutation({
  args: patientInvitationArgs,
  returns: v.object({
    invitationId: v.id("patient_invitations"),
    email: v.string(),
    clerkOrganizationId: v.string(),
    sent: v.boolean(),
  }),
  handler: async (ctx, args) => {
    const me = await requireRole(ctx, STAFF_ROLES);
    const parsed = invitationInput.safeParse({
      ...args,
      email: normalizedEmail(args.email),
    });
    if (!parsed.success) throw new ConvexError("invalid_invitation");
    const organization = await ctx.db.get(me.organizationId);
    if (!organization?.clerkOrgId || organization.deletedAt !== undefined)
      throw new ConvexError("invitation_unavailable");
    const { firstName, lastName, email } = parsed.data;
    const prior = await ctx.db
      .query("patient_invitations")
      .withIndex("by_organization_and_email", (q) =>
        q.eq("organizationId", me.organizationId).eq("email", email),
      )
      .unique();
    if (prior) {
      if (
        prior.requestedBy !== me._id &&
        me.role !== "admin" &&
        me.role !== "org_admin"
      )
        throw new ConvexError("invitation_unavailable");
      if (prior.status === "accepted")
        throw new ConvexError("invitation_unavailable");
      return {
        invitationId: prior._id,
        email,
        clerkOrganizationId: organization.clerkOrgId,
        sent: prior.status === "sent",
      };
    }
    const matches = (await ctx.db.query("users").collect()).filter(
      (user) => normalizedEmail(user.email) === email,
    );
    const ambiguous = matches.length > 1;
    const held = ambiguous ? undefined : matches[0];
    if (
      held &&
      (await organizationRoleOf(ctx, held._id, me.organizationId)) !== null &&
      held.clerkId !== ""
    )
      throw new ConvexError("invitation_unavailable");
    if (
      held?.organizationId === me.organizationId &&
      !(await canAccessUser(ctx, held._id, me))
    )
      throw new ConvexError("invitation_unavailable");
    const now = Date.now();
    const userId = ambiguous
      ? undefined
      : (held?._id ??
        (await ctx.db.insert("users", {
          clerkId: "",
          organizationId: me.organizationId,
          role: "user",
          firstName,
          lastName,
          email,
          language: args.language ?? "fr",
          createdAt: now,
        })));
    if (!held && userId)
      await ctx.db.insert("memberships", {
        userId,
        organizationId: me.organizationId,
        role: "user",
        active: false,
      });
    const gestionnaireIds: Id<"users">[] = [];
    for (const id of me.role === "gestionnaire"
      ? [me._id]
      : (args.gestionnaireIds ?? [])) {
      if (
        (await organizationRoleOf(ctx, id, me.organizationId)) ===
          "gestionnaire" &&
        !gestionnaireIds.includes(id)
      )
        gestionnaireIds.push(id);
    }
    const invitationId = await ctx.db.insert("patient_invitations", {
      organizationId: me.organizationId,
      userId,
      email,
      requestedBy: me._id,
      gestionnaireIds,
      status: "pending",
      createdAt: now,
      updatedAt: now,
    });
    await audit(ctx, {
      actor: userActor(me),
      source: "site",
      organizationId: me.organizationId,
      action: "user.invited",
      targetId: invitationId,
      details: {
        role: "user",
        added: gestionnaireIds,
        ...(userId === undefined ? {} : { userId }),
      },
    });
    return {
      invitationId,
      email,
      clerkOrganizationId: organization.clerkOrgId,
      sent: false,
    };
  },
});

export const finish = internalMutation({
  args: {
    invitationId: v.id("patient_invitations"),
    clerkInvitationId: v.union(v.string(), v.null()),
  },
  returns: v.null(),
  handler: async (ctx, args) => {
    const row = await ctx.db.get(args.invitationId);
    if (!row || row.status === "accepted") return null;
    if (row.status === "sent" && args.clerkInvitationId === null) return null;
    await ctx.db.patch(row._id, {
      status: args.clerkInvitationId === null ? "failed" : "sent",
      clerkInvitationId: args.clerkInvitationId ?? undefined,
      updatedAt: Date.now(),
    });
    return null;
  },
});
