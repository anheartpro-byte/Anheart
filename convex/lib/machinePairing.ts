/**
 * The rules of a pairing code (ANH-115, contract 1.17, `machine_pairing` of
 * `contracts/machine-api.json`): what a code is, how a typed one and a Pi's
 * serial number are read, and when a code may still be claimed.
 *
 * A code is drawn and hashed in `convex/pairingCodes.ts` (Node, for scrypt);
 * it is stored, claimed and counted in `convex/machinePairing.ts`.
 */
import { v } from "convex/values";
import machineApi from "../../contracts/machine-api.json";
import type { Doc } from "../_generated/dataModel";

const SECTION = machineApi.machine_pairing;

/** The 32 symbols of a code (Crockford's base 32): 5 bits each. */
export const PAIRING_ALPHABET: string = SECTION.alphabet;
export const PAIRING_CODE_LENGTH: number = SECTION.code_length;
export const PAIRING_GROUP_LENGTH: number = SECTION.group_length;
export const PAIRING_TTL_MS: number = SECTION.ttl_s * 1000;
/** Wrong codes a code takes for its serial number before it is burned. */
export const PAIRING_MAX_FAILURES: number = SECTION.max_failures;
export const PAIRING_MAX_SERIAL_CHARS: number = SECTION.max_serial_chars;
/** The end of the host of an address a console's screen accepts: one of Anheart's deployments. */
export const PAIRING_TYPED_HOST_SUFFIX: string = SECTION.typed_address_host_suffix;
export const PAIRING_MAX_BODY_BYTES: number = SECTION.max_body_bytes;
export const PAIRING_GLOBAL_LIMIT = SECTION.global_limit;

/**
 * scrypt of a code: N = 2^15, r = 8, p = 1, the parameters of the operators'
 * console codes. One hash takes 128 x N x r = 32 MiB and some 50 to 100 ms on
 * one core: far inside a Node action's 512 MiB and its minutes, and once per
 * pairing request, which the global bucket holds to twenty a minute. Against
 * a copy of the table, 2^40 codes at that cost outlast a code's 30 minutes
 * by far more than a lifetime.
 */
export const PAIRING_SCRYPT = {
  n: 32768,
  r: 8,
  p: 1,
  saltBytes: 16,
  hashBytes: 32,
} as const;

const TYPED_SEPARATORS = /[\s-]+/g;
const CODE_PATTERN = new RegExp(
  `^[${PAIRING_ALPHABET}]{${PAIRING_CODE_LENGTH}}$`,
);
const SERIAL_PATTERN = /^[0-9a-z][0-9a-z._:-]*$/;

/**
 * A code as it is hashed, from a code as someone typed it: upper case, no
 * spaces nor dashes, O read 0, I and L read 1. Null when it is not a code.
 */
export function normalizedCode(typed: unknown): string | null {
  if (typeof typed !== "string" || typed.length > 4 * PAIRING_CODE_LENGTH)
    return null;
  const code = typed
    .toUpperCase()
    .replace(TYPED_SEPARATORS, "")
    .replace(/O/g, "0")
    .replace(/[IL]/g, "1");
  return CODE_PATTERN.test(code) ? code : null;
}

/** A code as it is shown: its groups, separated by a dash. */
export function shownCode(code: string): string {
  const groups: string[] = [];
  for (let at = 0; at < code.length; at += PAIRING_GROUP_LENGTH)
    groups.push(code.slice(at, at + PAIRING_GROUP_LENGTH));
  return groups.join("-");
}

/**
 * A Pi's serial number as it is compared: no leading or trailing space, lower
 * case, at most `max_serial_chars` characters of digits, letters and `. _ : -`.
 * Null when it is none.
 */
export function normalizedSerial(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const serial = raw.trim().toLowerCase();
  return serial.length <= PAIRING_MAX_SERIAL_CHARS &&
    SERIAL_PATTERN.test(serial)
    ? serial
    : null;
}

/** Whether a code may still be claimed at `now`: open, not expired, not burned. */
export function pairingIsLive(
  code: Pick<
    Doc<"machine_pairing_codes">,
    "closedAt" | "expiresAt" | "failures"
  >,
  now: number,
): boolean {
  return (
    code.closedAt === undefined &&
    code.expiresAt > now &&
    code.failures < PAIRING_MAX_FAILURES
  );
}

/** What a claim answers (`machinePairing.claimPairing`), and the verification with it. */
export const claimOutcome = v.union(
  v.object({
    status: v.literal("claimed"),
    machineId: v.id("machines"),
    // The machine's new key: in this answer only, on its way to the console.
    apiKey: v.string(),
    keyPrefix: v.string(),
  }),
  v.object({ status: v.literal("refused") }),
);
