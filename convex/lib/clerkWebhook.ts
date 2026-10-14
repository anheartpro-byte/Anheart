import { v, type Infer } from "convex/values";
import { z } from "zod";

const time = z.number().int().nonnegative().max(Number.MAX_SAFE_INTEGER);
const identifier = z.string().min(1).max(256);
const organization = z.object({
  id: identifier,
  name: z.string().min(1).max(1024),
  slug: z.string().min(1).max(256).nullable(),
  created_at: time,
  updated_at: time,
});
const membership = z.object({
  id: identifier,
  role: z.enum(["org:admin", "org:gestionnaire", "org:patient"]),
  updated_at: time,
  organization,
  public_user_data: z.object({
    user_id: identifier,
    first_name: z.string().max(1024).nullable(),
    last_name: z.string().max(1024).nullable(),
    identifier: z.string().max(1024),
  }),
});

const snapshot = v.object({
  id: v.string(),
  name: v.string(),
  slug: v.string(),
  createdAt: v.number(),
  updatedAt: v.number(),
});
export const clerkEventValidator = v.union(
  v.object({
    kind: v.literal("userDeleted"),
    userId: v.string(),
    occurredAt: v.number(),
    updatedAt: v.number(),
  }),
  v.object({
    kind: v.literal("organization"),
    deleted: v.boolean(),
    organization: snapshot,
    occurredAt: v.number(),
    updatedAt: v.number(),
  }),
  v.object({
    kind: v.literal("membership"),
    deleted: v.boolean(),
    organization: snapshot,
    occurredAt: v.number(),
    updatedAt: v.number(),
    membershipId: v.string(),
    userId: v.string(),
    firstName: v.string(),
    lastName: v.string(),
    email: v.string(),
    verifiedEmails: v.optional(v.array(v.string())),
    role: v.union(
      v.literal("admin"),
      v.literal("gestionnaire"),
      v.literal("user"),
    ),
  }),
);
export type ClerkEvent = Infer<typeof clerkEventValidator>;

function snapshotOf(value: z.infer<typeof organization>) {
  return {
    id: value.id,
    name: value.name,
    slug: value.slug ?? value.id,
    createdAt: value.created_at,
    updatedAt: value.updated_at,
  };
}

export function parseClerkEvent(value: unknown): ClerkEvent | "ignored" | null {
  const envelope = z
    .object({ type: z.string(), timestamp: time, data: z.unknown() })
    .safeParse(value);
  if (!envelope.success) return null;
  const { type, timestamp, data } = envelope.data;
  if (type === "user.deleted") {
    const parsed = z.object({ id: identifier }).safeParse(data);
    return parsed.success
      ? {
          kind: "userDeleted",
          userId: parsed.data.id,
          occurredAt: timestamp,
          updatedAt: timestamp,
        }
      : null;
  }
  if (type === "organization.deleted") {
    const parsed = z.object({ id: identifier }).safeParse(data);
    if (!parsed.success) return null;
    return {
      kind: "organization",
      deleted: true,
      occurredAt: timestamp,
      updatedAt: timestamp,
      organization: {
        id: parsed.data.id,
        name: "Deleted organization",
        slug: `deleted-${parsed.data.id}`,
        createdAt: timestamp,
        updatedAt: timestamp,
      },
    };
  }
  if (type === "organization.created" || type === "organization.updated") {
    const parsed = organization.safeParse(data);
    if (!parsed.success) return null;
    return {
      kind: "organization",
      deleted: false,
      occurredAt: timestamp,
      updatedAt: parsed.data.updated_at,
      organization: snapshotOf(parsed.data),
    };
  }
  if (
    [
      "organizationMembership.created",
      "organizationMembership.updated",
      "organizationMembership.deleted",
    ].includes(type)
  ) {
    const parsed = membership.safeParse(data);
    if (!parsed.success) return null;
    const m = parsed.data;
    const email = z.email().safeParse(m.public_user_data.identifier);
    const deleted = type === "organizationMembership.deleted";
    return {
      kind: "membership",
      deleted,
      occurredAt: timestamp,
      updatedAt: deleted ? timestamp : m.updated_at,
      organization: snapshotOf(m.organization),
      membershipId: m.id,
      userId: m.public_user_data.user_id,
      firstName: m.public_user_data.first_name ?? "",
      lastName: m.public_user_data.last_name ?? "",
      email: email.success ? email.data : "",
      role:
        m.role === "org:admin"
          ? "admin"
          : m.role === "org:gestionnaire"
            ? "gestionnaire"
            : "user",
    };
  }
  return "ignored";
}

export async function verifiedClerkEmails(
  userId: string,
): Promise<string[] | null> {
  const secret = process.env.CLERK_SECRET_KEY;
  if (!secret) return null;
  try {
    const response = await fetch(
      `https://api.clerk.com/v1/users/${encodeURIComponent(userId)}`,
      {
        headers: { Authorization: `Bearer ${secret}` },
        signal: AbortSignal.timeout(10000),
      },
    );
    if (!response.ok) return null;
    const result = z
      .object({
        id: identifier,
        email_addresses: z.array(
          z.object({
            email_address: z.email(),
            verification: z.object({ status: z.string() }).nullable(),
          }),
        ),
      })
      .safeParse(await response.json());
    if (!result.success || result.data.id !== userId) return null;
    return result.data.email_addresses
      .filter((email) => email.verification?.status === "verified")
      .map((email) => email.email_address);
  } catch {
    return null;
  }
}

function unbase64(text: string): Uint8Array<ArrayBuffer> | null {
  try {
    return Uint8Array.from(atob(text), (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

export async function verifyClerkSignature(
  headers: Headers,
  body: string,
  secret: string,
  now: number,
): Promise<string | null> {
  const id = headers.get("svix-id");
  const stamp = headers.get("svix-timestamp");
  const signatures = headers.get("svix-signature");
  if (
    !id ||
    id.length > 256 ||
    !stamp ||
    !/^\d{1,16}$/.test(stamp) ||
    !signatures ||
    signatures.length > 4096
  )
    return null;
  const timestamp = Number(stamp);
  if (
    !Number.isSafeInteger(timestamp) ||
    Math.abs(now / 1000 - timestamp) > 300
  )
    return null;
  if (!secret.startsWith("whsec_")) return null;
  const keyBytes = unbase64(secret.slice(6));
  if (!keyBytes || keyBytes.length === 0) return null;
  const key = await crypto.subtle.importKey(
    "raw",
    keyBytes,
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["verify"],
  );
  const content = new TextEncoder().encode(`${id}.${stamp}.${body}`);
  for (const signature of signatures.split(/\s+/)) {
    if (!signature.startsWith("v1,")) continue;
    const bytes = unbase64(signature.slice(3));
    if (
      bytes?.length === 32 &&
      (await crypto.subtle.verify("HMAC", key, bytes, content))
    )
      return id;
  }
  return null;
}

export async function readWebhookBody(
  request: Request,
): Promise<string | null> {
  if (!request.body) return null;
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      size += part.value.byteLength;
      if (size > 1024 * 1024) {
        await reader.cancel();
        return null;
      }
      chunks.push(part.value);
    }
    const bytes = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      bytes.set(chunk, offset);
      offset += chunk.length;
    }
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    return null;
  } finally {
    reader.releaseLock();
  }
}
