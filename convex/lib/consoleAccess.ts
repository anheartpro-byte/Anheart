/**
 * Who may act at a machine's console (ANH-152): the rules of the operator list
 * and of the tablets, and the signed text the machines are sent.
 *
 * An operator is a member of the machine's organisation (admin or
 * gestionnaire) whom an admin enabled on that machine, and who set their own
 * console code on the site. The code is hashed with scrypt in a Node action
 * (`convex/consoleCodes.ts`) and never shown to anyone, the admin included; the
 * console verifies it offline against the hash of the list it holds.
 *
 * A tablet is a device token created on the site for one machine, shown once,
 * revocable. The list carries its SHA-256: the token is 32 random bytes, so a
 * plain digest suffices where a 6-digit code needs a slow hash.
 *
 * The list goes down on the catalogue's signed channel
 * (`operator_list` of contracts/machine-api.json, contract 1.13).
 */
import { v, type Infer } from "convex/values";
import machineApi from "../../contracts/machine-api.json";

const SECTION = machineApi.operator_list;

/** What a signed operator list says it is: never a catalogue read as a list. */
export const OPERATOR_LIST_KIND = SECTION.kind;
/** The scrypt parameters of a new code (`code` of the shared file). */
export const OPERATOR_CODE = SECTION.code;
export const MAX_OPERATORS = SECTION.max_operators;
export const MAX_DEVICES = SECTION.max_devices;
export const MAX_NAME_CHARS = SECTION.max_name_chars;
/** The largest signed text a console is sent (`max_payload_bytes`). */
export const MAX_OPERATOR_LIST_BYTES = SECTION.max_payload_bytes;
/** Who an event names when a tablet acted with nobody signed in. */
export const DEVICE_ACTOR_PREFIX = SECTION.device_actor_prefix;

/** The roles of an organisation whose members may be enabled as operators. */
export const OPERATOR_ROLES: readonly string[] = ["admin", "gestionnaire"];

/** A code as stored and as signed: the parameters, the salt and the hash, in base64. */
export const operatorCodeValidator = v.object({
  algorithm: v.literal("scrypt"),
  n: v.number(),
  r: v.number(),
  p: v.number(),
  salt: v.string(),
  hash: v.string(),
});
export type OperatorCode = Infer<typeof operatorCodeValidator>;

/**
 * Whether `code` may be a console code: `min_digits` to `max_digits` digits,
 * neither one digit repeated nor a run up or down (123456, 987654). The
 * lockout after five failures is what protects a code this short; refusing
 * the first codes anyone would try costs nothing.
 */
export function isAcceptableCode(code: string): boolean {
  const digits = new RegExp(
    `^[0-9]{${OPERATOR_CODE.min_digits},${OPERATOR_CODE.max_digits}}$`,
  );
  if (!digits.test(code)) return false;
  const steps = new Set(
    Array.from(code.slice(1), (digit, i) => (Number(digit) - Number(code[i]) + 10) % 10),
  );
  return !(steps.size === 1 && [0, 1, 9].includes([...steps][0]));
}

/** A tablet's name: 1 to `max_name_chars` characters once trimmed. */
export function deviceNameOf(name: string): string | null {
  const trimmed = name.trim();
  return trimmed.length > 0 && Array.from(trimmed).length <= MAX_NAME_CHARS
    ? trimmed
    : null;
}

/** A name as the console shows it: bounded, never empty. */
export function shownName(name: string, fallback: string): string {
  const trimmed = name.trim() || fallback;
  return Array.from(trimmed).slice(0, MAX_NAME_CHARS).join("");
}

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The SHA-256 of a token, in hexadecimal: what the list carries of it. */
export async function tokenSha256(token: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(token),
  );
  return hex(new Uint8Array(digest));
}

/** A new tablet token, `anhd1.` and 64 hexadecimal digits, with its digest and its prefix. */
export async function newDeviceToken(): Promise<{
  token: string;
  sha256: string;
  prefix: string;
}> {
  const secret = hex(crypto.getRandomValues(new Uint8Array(32)));
  const token = `anhd1.${secret}`;
  return { token, sha256: await tokenSha256(token), prefix: token.slice(0, 14) };
}

export type ListedOperator = { userId: string; name: string; code: OperatorCode };
export type ListedDevice = { deviceId: string; name: string; tokenSha256: string };

const byKey = <T>(key: (item: T) => string) => (a: T, b: T) =>
  key(a) < key(b) ? -1 : key(a) > key(b) ? 1 : 0;

/**
 * The text a machine is sent signed: its id, the server's clock, its operators
 * and its tablets, each list sorted. Its bytes are what is signed.
 */
export function operatorListPayload(list: {
  machineId: string;
  issuedAt: number;
  operators: readonly ListedOperator[];
  devices: readonly ListedDevice[];
}): string {
  return JSON.stringify({
    kind: OPERATOR_LIST_KIND,
    machineId: list.machineId,
    issuedAt: list.issuedAt,
    operators: [...list.operators]
      .sort(byKey((o) => o.userId))
      .map((o) => ({ userId: o.userId, name: o.name, code: o.code })),
    devices: [...list.devices]
      .sort(byKey((d) => d.deviceId))
      .map((d) => ({ deviceId: d.deviceId, name: d.name, tokenSha256: d.tokenSha256 })),
  });
}
