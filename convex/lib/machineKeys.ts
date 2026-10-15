/**
 * The keys of a machine (ANH-165): what a key is worth at a given instant,
 * how one is issued, how a rotation retires the keys it replaces, and how the
 * single key a machine carried before (`machines.apiKey`) is moved into the
 * table `machine_api_keys`.
 *
 * A key is `anh1.<selector>.<secret>` (convex/lib/crypto.ts). The table keeps
 * its public selector, a salted verifier of the whole key and a prefix to
 * show, never the key: the plain key leaves the server once, in the answer of
 * the call that issued it.
 */
import type { Doc, Id } from "../_generated/dataModel";
import type { MutationCtx } from "../_generated/server";
import { refusal } from "./errors";
import { generateApiKey, isApiKeyVerifier } from "./crypto";

/** A use of a key is written at most this often (`lastUsedAt`). */
export const KEY_USE_REFRESH_MS = 60_000;
/** The overlap of a rotation when none is asked for: 24 h. */
export const DEFAULT_OVERLAP_MINUTES = 24 * 60;
/** The longest overlap a rotation may ask for: 7 days. */
export const MAX_OVERLAP_MINUTES = 7 * 24 * 60;
/** The shortest lifetime of a new key: one hour. */
export const MIN_KEY_LIFETIME_MINUTES = 60;
/** The longest lifetime of a new key: ten years (365-day years). */
export const MAX_KEY_LIFETIME_MINUTES = 10 * 365 * 24 * 60;
/** Keys listed on a machine's page, most recent first. */
export const LISTED_KEYS = 50;
/** Expired keys the job revokes in one run. */
export const EXPIRED_KEYS_PER_RUN = 100;

const MINUTE_MS = 60_000;

type Key = Doc<"machine_api_keys">;

/** What the dashboard shows of a key: `anh1.` and 8 characters of its selector. */
export function keyPrefix(selector: string): string {
  return `anh1.${selector.slice(0, 8)}`;
}

/** Whether the key opens the machine routes at `now`. */
export function keyIsValid(key: Key, now: number): boolean {
  return (
    key.revokedAt === undefined &&
    (key.expiresAt === undefined || now < key.expiresAt)
  );
}

/** Whether the use of the key at `now` is to be written (once a minute at most). */
export function keyUseIsDue(key: Key, now: number): boolean {
  return (
    key.lastUsedAt === undefined || now - key.lastUsedAt >= KEY_USE_REFRESH_MS
  );
}

/**
 * How the key stands at `now`, as the machine's page shows it: revoked,
 * expired, replaced by a newer key but still valid (the overlap), or active.
 */
export function keyState(
  key: Key,
  now: number,
): "active" | "replaced" | "expired" | "revoked" {
  if (key.revokedAt !== undefined) return "revoked";
  if (!keyIsValid(key, now)) return "expired";
  return key.replacedBy === undefined ? "active" : "replaced";
}

/** What a rotation is asked for, checked. */
export type RotationOptions = {
  /** How long the keys it replaces stay valid, in ms; 0 revokes them at once. */
  overlapMs: number;
  /** When the new key expires; undefined for never. */
  expiresAt: number | undefined;
};

/** Whether `value` is a whole number between `min` and `max`. */
function wholeBetween(value: number, min: number, max: number): boolean {
  return Number.isInteger(value) && value >= min && value <= max;
}

/**
 * The options of a new key, or the refusal of options that are not: an
 * overlap that is not a whole number of minutes from 0 to 7 days
 * (`invalid_key_overlap`), a lifetime that is not a whole number of minutes
 * from one hour to ten years (`invalid_key_expiry`). The expiry is counted
 * from `now`, on the server's clock: never on the clock of the browser that
 * asked for it.
 */
export function rotationOptions(
  args: { overlapMinutes?: number; lifetimeMinutes?: number },
  now: number,
): RotationOptions {
  const overlapMinutes = args.overlapMinutes ?? DEFAULT_OVERLAP_MINUTES;
  if (!wholeBetween(overlapMinutes, 0, MAX_OVERLAP_MINUTES)) {
    throw refusal("invalid_key_overlap");
  }
  const { lifetimeMinutes } = args;
  if (
    lifetimeMinutes !== undefined &&
    !wholeBetween(
      lifetimeMinutes,
      MIN_KEY_LIFETIME_MINUTES,
      MAX_KEY_LIFETIME_MINUTES,
    )
  ) {
    throw refusal("invalid_key_expiry");
  }
  return {
    overlapMs: overlapMinutes * MINUTE_MS,
    expiresAt:
      lifetimeMinutes === undefined
        ? undefined
        : now + lifetimeMinutes * MINUTE_MS,
  };
}

/** A new key of the machine, written in the table; the plain key is returned, never stored. */
export async function insertMachineKey(
  ctx: MutationCtx,
  machineId: Id<"machines">,
  issued: {
    createdBy: Id<"users"> | undefined;
    expiresAt: number | undefined;
    now: number;
  },
): Promise<{
  keyId: Id<"machine_api_keys">;
  apiKey: string;
  prefix: string;
}> {
  const { plain, hashed, selector } = await generateApiKey();
  const prefix = keyPrefix(selector);
  const keyId = await ctx.db.insert("machine_api_keys", {
    machineId,
    selector,
    keyHash: hashed,
    prefix,
    createdAt: issued.now,
    createdBy: issued.createdBy,
    expiresAt: issued.expiresAt,
  });
  return { keyId, apiKey: plain, prefix };
}

/**
 * Issue a new key for the machine and retire every key it holds that is
 * still valid: each one stays valid until the end of the overlap (sooner if
 * it expired sooner), then the job `revokeExpiredKeys` revokes it; with no
 * overlap, each one is revoked here, at once. A key the machine still carries
 * in `machines.apiKey` is moved into the table first, and retired the same
 * way.
 */
export async function rotateMachineKey(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  options: RotationOptions & {
    createdBy: Id<"users"> | undefined;
    now: number;
  },
): Promise<{
  keyId: Id<"machine_api_keys">;
  apiKey: string;
  prefix: string;
}> {
  const { now } = options;
  await moveLegacyKey(ctx, machine, now);
  const current = await ctx.db
    .query("machine_api_keys")
    .withIndex("by_machine_and_revoked", (q) =>
      q.eq("machineId", machine._id).eq("revokedAt", undefined),
    )
    .collect();
  const issued = await insertMachineKey(ctx, machine._id, {
    createdBy: options.createdBy,
    expiresAt: options.expiresAt,
    now,
  });
  const overlapEnd = now + options.overlapMs;
  for (const key of current) {
    // An expired key is left to the job: there is nothing to shorten.
    if (!keyIsValid(key, now)) continue;
    if (options.overlapMs === 0) {
      await ctx.db.patch(key._id, {
        revokedAt: now,
        revokedBy: options.createdBy,
        replacedBy: issued.keyId,
      });
    } else {
      await ctx.db.patch(key._id, {
        expiresAt: Math.min(key.expiresAt ?? overlapEnd, overlapEnd),
        replacedBy: issued.keyId,
      });
    }
  }
  return issued;
}

/**
 * Move the single key of a machine written before ANH-165 into the table,
 * then clear `machines.apiKey` and `machines.apiKeySelector`. The same key
 * goes on opening the machine routes, now as a row of the table.
 *
 * A legacy value that is not a verifier with its selector (the reversible
 * encodings refused since ANH-121) opens nothing: it is cleared, not moved.
 * Idempotent: a machine without these fields is left as it is.
 */
export async function moveLegacyKey(
  ctx: MutationCtx,
  machine: Doc<"machines">,
  now: number,
): Promise<"moved" | "discarded" | "none"> {
  const { apiKey, apiKeySelector: selector } = machine;
  if (apiKey === undefined && selector === undefined) return "none";
  let outcome: "moved" | "discarded" = "discarded";
  if (
    apiKey !== undefined &&
    selector !== undefined &&
    /^[a-f0-9]{32}$/.test(selector) &&
    isApiKeyVerifier(apiKey)
  ) {
    const already = await ctx.db
      .query("machine_api_keys")
      .withIndex("by_selector", (q) => q.eq("selector", selector))
      .first();
    if (already === null) {
      await ctx.db.insert("machine_api_keys", {
        machineId: machine._id,
        selector,
        keyHash: apiKey,
        prefix: keyPrefix(selector),
        // When the key entered the table: the day it was issued is not known.
        createdAt: now,
      });
    }
    outcome = "moved";
  }
  await ctx.db.patch(machine._id, {
    apiKey: undefined,
    apiKeySelector: undefined,
  });
  return outcome;
}
