/**
 * Rate limits and body sizes (ANH-166): a machine or an account that sends
 * too much is answered "wait", before anything it sent is read or written.
 *
 * The state of every limit lives in the rate limiter component
 * (`@convex-dev/rate-limiter`, mounted by `convex/convex.config.ts`), one
 * token bucket per limit and per key. A refused call writes nothing there:
 * only an accepted one takes a token.
 *
 * Per machine: heartbeat, training/poll, telemetry, events, local, profiles,
 * records/upload-url and commit (ANH-228). Their limits and their body
 * sizes are read from the shared contract (`contracts/machine-api.json`,
 * `rate_limits` and `max_body_bytes`), which the Pi's tests pin its own
 * cadences and batch sizes to: a console that works as designed never meets
 * them. Over the limit, a route answers 429
 * `{error: "rate_limited", message, retryAfterMs}` with `Retry-After`.
 * Never rate limited: training/status (the stop), training/end (the end),
 * training/start (only writes for a pending launch, already bounded by
 * launchAutoSession; a 429 would stop an armed console from before 1.5),
 * GET records/status and GET roster (read only; an invalid key, which cannot
 * be rate limited, already costs as much).
 *
 * Per account (the subject of its token): `launchAutoSession`, and every
 * public mutation that writes, together. Over the limit, the call throws a
 * `ConvexError` `{code: "rate_limited", message, retryAfterMs}`. A stop
 * (`training.requestStop`) is never limited: refusing a stop is never the
 * safe side.
 */
import { MINUTE, RateLimiter } from "@convex-dev/rate-limiter";
import { ConvexError } from "convex/values";
import machineApi from "../../contracts/machine-api.json";
import { components } from "../_generated/api";
import type { ActionCtx, MutationCtx } from "../_generated/server";
import { machineErrorResponse } from "./contract";

type TokenBucket = { rate: number; period_ms: number; capacity: number };

const routes: Record<string, TokenBucket> = machineApi.rate_limits.routes;
const maxBytes: Record<string, number> = machineApi.max_body_bytes.routes;

/** The token bucket of a machine route, as the shared contract states it. */
function bucket(route: string) {
  const limit = routes[route];
  if (limit === undefined) throw new Error(`No rate limit for ${route}`);
  return {
    kind: "token bucket" as const,
    rate: limit.rate,
    period: limit.period_ms,
    capacity: limit.capacity,
  };
}

/** The largest body of a machine route, in bytes, as the shared contract states it. */
function sizeOf(route: string): number {
  const size = maxBytes[route];
  if (size === undefined) throw new Error(`No body size for ${route}`);
  return size;
}

export const rateLimiter = new RateLimiter(components.rateLimiter, {
  machineHeartbeat: bucket("POST /api/machine/heartbeat"),
  machinePoll: bucket("GET /api/machine/training/poll"),
  machineTelemetry: bucket("POST /api/machine/training/telemetry"),
  machineEvents: bucket("POST /api/machine/training/events"),
  machineUploadUrl: bucket("POST /api/machine/records/upload-url"),
  machineLocalSession: bucket("POST /api/machine/training/local"),
  machineProfiles: bucket("POST /api/machine/profiles"),
  machineRecordCommit: bucket("POST /api/machine/records/commit"),
  /** Remote launches by one account: five a minute. */
  launchAutoSession: {
    kind: "token bucket",
    rate: 5,
    period: MINUTE,
    capacity: 5,
  },
  /** Every other write of one account, launches included: sixty a minute. */
  userWrite: { kind: "token bucket", rate: 60, period: MINUTE, capacity: 60 },
});

/** The limits of the machine routes. */
export type MachineLimit =
  | "machineHeartbeat"
  | "machinePoll"
  | "machineTelemetry"
  | "machineEvents"
  | "machineUploadUrl"
  | "machineLocalSession"
  | "machineProfiles"
  | "machineRecordCommit";

/** The limits of an account. */
export type UserLimit = "launchAutoSession" | "userWrite";

export const MAX_HEARTBEAT_BYTES = sizeOf("POST /api/machine/heartbeat");
export const MAX_TELEMETRY_BYTES = sizeOf(
  "POST /api/machine/training/telemetry",
);
export const MAX_EVENTS_BYTES = sizeOf("POST /api/machine/training/events");

/** Milliseconds to wait, as a whole number never below zero. */
function waitOf(retryAfter: number): number {
  return Math.max(0, Math.ceil(retryAfter));
}

/** The 429 of a machine route: the code, the wait in ms, and `Retry-After` in seconds. */
export function rateLimitedResponse(retryAfter: number): Response {
  const retryAfterMs = waitOf(retryAfter);
  return machineErrorResponse(
    429,
    "rate_limited",
    "Too many requests from this machine on this route: retry later",
    { retryAfterMs },
    { "Retry-After": String(Math.ceil(retryAfterMs / 1000)) },
  );
}

/** The 413 of a machine route whose body is over its size. */
export function payloadTooLarge(message: string): Response {
  return machineErrorResponse(413, "payload_too_large", message);
}

/**
 * Take a token of a machine route's limit. Null when the request may go on,
 * the 429 to answer otherwise.
 */
export async function limitMachine(
  ctx: ActionCtx,
  name: MachineLimit,
  machineId: string,
): Promise<Response | null> {
  const status = await rateLimiter.limit(ctx, name, { key: machineId });
  return status.ok ? null : rateLimitedResponse(status.retryAfter);
}

/** What an account over its limit is thrown. */
export function rateLimitedError(retryAfter: number) {
  return new ConvexError({
    code: "rate_limited",
    message: "Too many requests: retry later",
    retryAfterMs: waitOf(retryAfter),
  });
}

/**
 * Take a token of an account's limit, or throw `rate_limited`. A call with no
 * identity takes none: the function refuses it on its own, writing nothing.
 * Called inside a mutation, the token is given back if the mutation then
 * fails: only a write that happens is counted.
 */
export async function limitUser(
  ctx: MutationCtx | ActionCtx,
  name: UserLimit = "userWrite",
): Promise<void> {
  const identity = await ctx.auth.getUserIdentity();
  if (identity === null) return;
  const status = await rateLimiter.limit(ctx, name, { key: identity.subject });
  if (!status.ok) throw rateLimitedError(status.retryAfter);
}

/**
 * The body of a request as text, when it is at most `max` bytes; null when it
 * is longer. Neither is the JSON read here, nor more than `max + 1` bytes: a
 * `Content-Length` over the size is refused before a byte is read, and a body
 * without one is read until it passes the size.
 */
export async function boundedText(
  req: Request,
  max: number,
): Promise<string | null> {
  const declared = req.headers.get("Content-Length");
  if (declared !== null && /^\d+$/.test(declared) && Number(declared) > max) {
    return null;
  }
  if (req.body === null) return "";
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.byteLength;
    if (size > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return new TextDecoder().decode(bytes);
}
