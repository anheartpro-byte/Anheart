"use node";
/**
 * Pairing codes drawn and verified here, with scrypt (ANH-115): the default
 * runtime has WebCrypto, which has no memory-hard hash, and this one has
 * `node:crypto`. Parameters and why they fit: `PAIRING_SCRYPT`
 * (convex/lib/machinePairing.ts). The rows and the claim are
 * `convex/machinePairing.ts`.
 *
 * A code exists in clear in the answer of `issuePairingCode`, the one time it
 * is shown, and in the request of the console that sends it back: it is
 * neither stored, nor logged, nor audited.
 */
import { randomBytes, scrypt, timingSafeEqual } from "node:crypto";
import { v, type Infer } from "convex/values";
import { action, internalAction } from "./_generated/server";
import { internal } from "./_generated/api";
import type { Id } from "./_generated/dataModel";
import {
  claimOutcome,
  PAIRING_ALPHABET,
  PAIRING_CODE_LENGTH,
  PAIRING_SCRYPT,
  shownCode,
} from "./lib/machinePairing";

function hashed(code: string, salt: Buffer): Promise<Buffer> {
  const { n, r, p, hashBytes } = PAIRING_SCRYPT;
  return new Promise((resolve, reject) => {
    scrypt(
      code,
      salt,
      hashBytes,
      { N: n, r, p, maxmem: 256 * n * r },
      (error, key) => (error === null ? resolve(key) : reject(error)),
    );
  });
}

/** A new code: each character the five low bits of a random byte (32 divides 256, no bias). */
function drawnCode(): string {
  return Array.from(
    randomBytes(PAIRING_CODE_LENGTH),
    (byte) => PAIRING_ALPHABET[byte & 31],
  ).join("");
}

/**
 * Issue a pairing code for the machine and answer it, the only time it is
 * shown, with when it expires (30 minutes). Any open code of the machine is
 * replaced. Admins of the machine's organisation and the Anheart admin only,
 * as a key (`machineKeys.createMachineKey`); refused, with nothing written,
 * for a machine whose registry holds no Pi serial number
 * (`pairing_serial_missing`) or whose number another machine's open code
 * serves (`pairing_serial_in_use`).
 */
export const issuePairingCode = action({
  args: { machineId: v.id("machines") },
  returns: v.object({ code: v.string(), expiresAt: v.number() }),
  handler: async (ctx, args): Promise<{ code: string; expiresAt: number }> => {
    await ctx.runMutation(internal.machinePairing.prepareIssue, {
      machineId: args.machineId,
    });
    const code = drawnCode();
    const salt = randomBytes(PAIRING_SCRYPT.saltBytes);
    const hash = await hashed(code, salt);
    const { expiresAt }: { expiresAt: number } = await ctx.runMutation(
      internal.machinePairing.storeIssuedCode,
      {
        machineId: args.machineId,
        salt: salt.toString("base64"),
        hash: hash.toString("base64"),
      },
    );
    return { code: shownCode(code), expiresAt };
  },
});

// A serial number with no open code costs the same hash as a known one: on
// this salt, compared with a hash no code has.
const NO_SALT = randomBytes(PAIRING_SCRYPT.saltBytes);
const NO_HASH = randomBytes(PAIRING_SCRYPT.hashBytes);

/**
 * The verdict on a code sent with a serial number, applied by the claim
 * (`machinePairing.claimPairing`), which reads the code again in its
 * transaction. Every request reads the open codes of its serial number,
 * hashes once and calls the claim, whether the number has an open code or
 * not: the answer and the scrypt work are the same. The claim of a known
 * number reads and writes the code (a failure counted), that of an unknown
 * one returns at once: the duration can still differ by that transaction,
 * a few milliseconds against the hash's tens. `code` and `serialPi` come as the route
 * read them (`normalizedCode`, `normalizedSerial`): anything else matches
 * nothing.
 */
export const verifyPairing = internalAction({
  args: { code: v.string(), serialPi: v.string() },
  returns: claimOutcome,
  handler: async (ctx, args): Promise<Infer<typeof claimOutcome>> => {
    const candidates: {
      pairingId: Id<"machine_pairing_codes">;
      salt: string;
      hash: string;
      expiresAt: number;
    }[] = await ctx.runQuery(internal.machinePairing.pairingCandidates, {
      serialPi: args.serialPi,
    });
    const now = Date.now();
    const open = candidates.find((candidate) => candidate.expiresAt > now);
    const expected =
      open === undefined ? NO_HASH : Buffer.from(open.hash, "base64");
    const actual = await hashed(
      args.code,
      open === undefined ? NO_SALT : Buffer.from(open.salt, "base64"),
    );
    const matched =
      open !== undefined &&
      actual.length === expected.length &&
      timingSafeEqual(actual, expected);
    return await ctx.runMutation(internal.machinePairing.claimPairing, {
      pairingId: open?.pairingId ?? null,
      matched,
    });
  },
});
