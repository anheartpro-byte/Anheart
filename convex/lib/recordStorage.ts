import { v } from "convex/values";
import { z } from "zod";

export const MAX_RECORD_FILE_BYTES = 16 * 1024 * 1024;
export const MAX_RECORD_FILES = 40_000;
export const MANIFEST_PAGE_SIZE = 1024;
export const MAX_MANIFEST_PAGES = 128;
export function recordBucket(path: string): number {
  let hash = 2166136261;
  for (const character of path)
    hash = Math.imul(hash ^ character.charCodeAt(0), 16777619) >>> 0;
  return hash % 64;
}
export const RECORD_MEDIA_PREFIX = "application/x-anheart-record;nonce=";
export const recordState = v.union(
  v.literal("none"),
  v.literal("partial"),
  v.literal("complete"),
  v.literal("rejected"),
);
export const recordPath = z
  .string()
  .max(160)
  .regex(/^(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$/);
export const uploadRequest = z
  .object({
    sessionId: z.string().min(1),
    path: recordPath,
    sha256: z.string().regex(/^[a-f0-9]{64}$/),
    size: z.number().int().min(0).max(MAX_RECORD_FILE_BYTES),
  })
  .refine((file) => file.path !== "manifest.json" || file.size <= 2 * 1024 * 1024);
export const commitRequest = uploadRequest.safeExtend({
  storageId: z.string().min(1),
});
export const depositManifest = z
  .object({
    schema_version: z.literal(2),
    ended_at: z.string().min(1),
    files: z.array(recordPath).min(3).max(MAX_RECORD_FILES),
  })
  .refine(
    ({ files }) =>
      new Set(files).size === files.length &&
      new TextEncoder().encode(JSON.stringify(files)).length <= 1024 * 1024 &&
      ["manifest.json", "ticks.csv", "events.jsonl", "checksums.sha256"].every(
        (path) => files.includes(path),
      ),
  );

export function uploadMediaType(nonce: string): string {
  return RECORD_MEDIA_PREFIX + nonce;
}
export function storageNonce(contentType: string | undefined): string | null {
  if (!contentType?.startsWith(RECORD_MEDIA_PREFIX)) return null;
  const nonce = contentType.slice(RECORD_MEDIA_PREFIX.length);
  return /^[a-f0-9]{64}$/.test(nonce) ? nonce : null;
}
export function randomNonce(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(32)), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
}
export async function sha256(bytes: ArrayBuffer): Promise<string> {
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", bytes)),
    (byte) => byte.toString(16).padStart(2, "0"),
  ).join("");
}
