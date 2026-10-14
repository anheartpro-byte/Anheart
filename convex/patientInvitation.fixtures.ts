import { vi } from "vitest";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import { NOW, type World } from "./test.setup";

export function mockClerkInvitations() {
  vi.stubEnv("CLERK_SECRET_KEY", "sk_test_synthetic");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init?: RequestInit) => {
      const body: unknown = JSON.parse(String(init?.body));
      if (
        typeof body !== "object" ||
        body === null ||
        !("email_address" in body)
      )
        throw new Error("Unexpected Clerk request");
      return Response.json({
        id: "orginv_synthetic",
        email_address: body.email_address,
        status: "pending",
        role: "org:patient",
      });
    }),
  );
}

export async function acceptPatientInvitation(
  w: World,
  invitationId: Id<"patient_invitations">,
  clerkId?: string,
) {
  const invitation = await w.t.run((ctx) => ctx.db.get(invitationId));
  if (!invitation) throw new Error("Missing invitation");
  const userId = invitation.userId;
  const user = userId ? await w.t.run((ctx) => ctx.db.get(userId)) : null;
  const organization = await w.t.run((ctx) =>
    ctx.db.get(invitation.organizationId),
  );
  if (!organization?.clerkOrgId) throw new Error("Missing Clerk organization");
  await w.t.mutation(internal.clerkWebhook.applyEvent, {
    eventId: `msg_${invitationId}`,
    event: {
      kind: "membership",
      deleted: false,
      occurredAt: NOW,
      updatedAt: NOW,
      organization: {
        id: organization.clerkOrgId,
        name: organization.name,
        slug: organization.slug,
        createdAt: NOW,
        updatedAt: NOW,
      },
      membershipId: `orgmem_${invitationId}`,
      userId: clerkId ?? (user?.clerkId || `user_${invitationId}`),
      firstName: "Clerk",
      lastName: "Patient",
      email: invitation.email,
      verifiedEmails: [invitation.email],
      role: "user",
    },
  });
  const accepted = await w.t.run((ctx) => ctx.db.get(invitationId));
  if (!accepted?.userId) throw new Error("Missing accepted profile");
  return accepted.userId;
}
