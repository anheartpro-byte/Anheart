import { v } from "convex/values";
import { z } from "zod";

export const patientInvitationArgs = {
  firstName: v.string(),
  lastName: v.string(),
  email: v.string(),
  language: v.optional(v.union(v.literal("fr"), v.literal("en"))),
  gestionnaireIds: v.optional(v.array(v.id("users"))),
};
export const invitationResult = v.object({
  invitationId: v.id("patient_invitations"),
  email: v.string(),
  status: v.literal("sent"),
});
export const invitationInput = z.object({
  firstName: z.string().trim().min(1).max(100),
  lastName: z.string().trim().min(1).max(100),
  email: z.email().max(254),
});
export function normalizedEmail(value: string): string {
  return value.trim().replace(/[A-Z]/g, (c) => c.toLowerCase());
}

const clerkInvitation = z.object({
  id: z.string(),
  email_address: z.string(),
  status: z.string(),
  role: z.string(),
});
const clerkPage = z.object({
  data: z.array(clerkInvitation),
  total_count: z.number(),
});

export async function sendPatientInvitation(
  secret: string,
  organizationId: string,
  email: string,
): Promise<string | null> {
  const url = `https://api.clerk.com/v1/organizations/${encodeURIComponent(organizationId)}/invitations`;
  const headers = {
    Authorization: `Bearer ${secret}`,
    "Content-Type": "application/json",
  };
  const signal = AbortSignal.timeout(15000);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ email_address: email, role: "org:patient" }),
      signal,
    });
    if (response.ok) {
      const parsed = clerkInvitation.safeParse(await response.json());
      return parsed.success &&
        parsed.data.status === "pending" &&
        parsed.data.role === "org:patient" &&
        normalizedEmail(parsed.data.email_address) === email
        ? parsed.data.id
        : null;
    }
    for (let offset = 0; offset < 10000; offset += 100) {
      const response = await fetch(
        `${url}?status=pending&limit=100&offset=${offset}`,
        { headers, signal },
      );
      if (!response.ok) return null;
      const parsed = clerkPage.safeParse(await response.json());
      if (!parsed.success) return null;
      const existing = parsed.data.data.find(
        (row) =>
          row.status === "pending" &&
          row.role === "org:patient" &&
          normalizedEmail(row.email_address) === email,
      );
      if (existing) return existing.id;
      if (
        offset + parsed.data.data.length >= parsed.data.total_count ||
        parsed.data.data.length === 0
      )
        return null;
    }
    return null;
  } catch {
    return null;
  }
}
