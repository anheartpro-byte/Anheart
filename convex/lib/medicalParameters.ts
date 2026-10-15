/**
 * Parse, validate, and verify the signed medical document used by Convex.
 * Its schema follows `raspberry-pi/src/medical.py`; age and cardiac limits
 * also follow `raspberry-pi/src/local_config.py`, and motion limits follow
 * `raspberry-pi/src/training/motion.py`. The version registry only grows so
 * each signed decision remains available for audit; current means greatest
 * semantic version.
 */
import type { Doc } from "../_generated/dataModel";
import type { MutationCtx, QueryCtx } from "../_generated/server";
import type { HrMaxFormula, MedicalParameters } from "../../lib/medicalParameters";

/** Public key used to verify shipped medical parameter signatures. */
export const MEDICAL_PUBLIC_KEY_PEM =
  "-----BEGIN PUBLIC KEY-----\n" +
  "MCowBQYDK2VwAyEA9lp8wwVIXK2xXJ6ljOYle/mDb4D3Y8QkSXTXi6V1kX0=\n" +
  "-----END PUBLIC KEY-----\n";

const TOP_KEYS = ["version", "issued_at", "signed_by", "parameters"];
const PARAMETER_KEYS = [
  "hr_hard_max_bpm",
  "hr_critical_bpm",
  "min_rider_age",
  "occupied_ceiling_g",
  "zone_ceiling_fraction",
  "hr_max_formula",
  "motion_limits",
  "recovery_min_s",
  "cooldown_min_s",
];
const MOTION_KEYS = [
  "output_rpm_per_s",
  "g_per_s",
  "min_run_motor_rpm",
  "max_interval_s",
];
const MEDICAL_VERSION_PATTERN =
  /^(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})$/;
const SIGNER_PATTERN = /^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$/;
const DATE_PATTERN = /^[0-9]{4}-[0-9]{2}-[0-9]{2}$/;

export function isMedicalVersion(value: unknown): value is string {
  return typeof value === "string" && MEDICAL_VERSION_PATTERN.test(value);
}

/** Validated fields Convex reads from a signed medical parameter document. */
export type ParsedMedicalDocument = MedicalParameters &
  Readonly<{ version: string; issuedAt: string; signedBy: string }>;

/** Status of a machine's reported medical version against the server reference. */
export type MedicalParametersStatus =
  | "unknown"
  | "unsigned"
  | "no_reference"
  | "current"
  | "obsolete"
  | "ahead";

function skipWhitespace(text: string, cursor: { value: number }): void {
  while (/\s/.test(text[cursor.value] ?? "")) cursor.value += 1;
}

function scanString(text: string, cursor: { value: number }): string {
  const start = cursor.value;
  cursor.value += 1;
  while (cursor.value < text.length) {
    const character = text[cursor.value];
    cursor.value += 1;
    if (character === "\\") {
      cursor.value += 1;
    } else if (character === '"') {
      const decoded: unknown = JSON.parse(text.slice(start, cursor.value));
      if (typeof decoded !== "string") throw new Error("Invalid JSON string");
      return decoded;
    }
  }
  throw new Error("Unterminated JSON string");
}

function rejectDuplicateMembers(text: string): void {
  const cursor = { value: 0 };
  const scanValue = (): void => {
    skipWhitespace(text, cursor);
    const character = text[cursor.value];
    if (character === '"') {
      scanString(text, cursor);
      return;
    }
    if (character === "{") {
      cursor.value += 1;
      skipWhitespace(text, cursor);
      const keys = new Set<string>();
      if (text[cursor.value] === "}") {
        cursor.value += 1;
        return;
      }
      while (cursor.value < text.length) {
        skipWhitespace(text, cursor);
        const key = scanString(text, cursor);
        if (keys.has(key)) throw new Error(`Duplicate JSON member: ${key}`);
        keys.add(key);
        skipWhitespace(text, cursor);
        cursor.value += 1;
        scanValue();
        skipWhitespace(text, cursor);
        if (text[cursor.value] === "}") {
          cursor.value += 1;
          return;
        }
        cursor.value += 1;
      }
      throw new Error("Unterminated JSON object");
    }
    if (character === "[") {
      cursor.value += 1;
      skipWhitespace(text, cursor);
      if (text[cursor.value] === "]") {
        cursor.value += 1;
        return;
      }
      while (cursor.value < text.length) {
        scanValue();
        skipWhitespace(text, cursor);
        if (text[cursor.value] === "]") {
          cursor.value += 1;
          return;
        }
        cursor.value += 1;
      }
      throw new Error("Unterminated JSON array");
    }
    while (
      cursor.value < text.length &&
      !/[\s,\]}]/.test(text[cursor.value] ?? "")
    ) {
      cursor.value += 1;
    }
  };
  scanValue();
}

function objectMembers(
  value: unknown,
  expected: readonly string[],
  path: string,
  optional: readonly string[] = [],
): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error(`${path} must be a JSON object`);
  }
  const members = value as Record<string, unknown>;
  for (const key of expected) {
    if (!Object.prototype.hasOwnProperty.call(members, key)) {
      throw new Error(`${path}.${key} is missing`);
    }
  }
  for (const key of Object.keys(members)) {
    if (!expected.includes(key) && !optional.includes(key)) {
      throw new Error(`${path}.${key} is unknown`);
    }
  }
  return members;
}

function positiveWhole(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0) {
    throw new Error(`${path} must be a positive whole number`);
  }
  return value;
}

function positiveFinite(value: unknown, path: string): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new Error(`${path} must be a positive finite number`);
  }
  return value;
}

function validDate(text: string): boolean {
  if (!DATE_PATTERN.test(text)) return false;
  const [yearText, monthText, dayText] = text.split("-");
  const year = Number(yearText);
  const month = Number(monthText);
  const day = Number(dayText);
  if (year < 1 || month < 1 || month > 12) return false;
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return day >= 1 && day <= (days[month - 1] ?? 0);
}

/** Parse and enforce the document shape and the console's accepted bounds. */
export function parseMedicalDocument(content: string): ParsedMedicalDocument {
  let decoded: unknown;
  try {
    decoded = JSON.parse(content) as unknown;
    rejectDuplicateMembers(content);
  } catch (error) {
    throw new Error(
      `Medical parameters document is not valid JSON: ${error instanceof Error ? error.message : String(error)}`,
    );
  }

  const document = objectMembers(decoded, TOP_KEYS, "document", ["_comment"]);
  if (
    Object.prototype.hasOwnProperty.call(document, "_comment") &&
    typeof document._comment !== "string"
  ) {
    throw new Error("document._comment must be text");
  }
  if (!isMedicalVersion(document.version)) {
    throw new Error("document.version must be MAJOR.MINOR.PATCH");
  }
  if (typeof document.issued_at !== "string" || !validDate(document.issued_at)) {
    throw new Error("document.issued_at must be a calendar date in YYYY-MM-DD format");
  }
  if (typeof document.signed_by !== "string" || !SIGNER_PATTERN.test(document.signed_by)) {
    throw new Error("document.signed_by must be a signer identifier");
  }

  const parameters = objectMembers(
    document.parameters,
    PARAMETER_KEYS,
    "document.parameters",
  );
  const hardMax = positiveWhole(
    parameters.hr_hard_max_bpm,
    "document.parameters.hr_hard_max_bpm",
  );
  const critical = positiveWhole(
    parameters.hr_critical_bpm,
    "document.parameters.hr_critical_bpm",
  );
  const minRiderAge = positiveWhole(
    parameters.min_rider_age,
    "document.parameters.min_rider_age",
  );
  const zoneCeilingFraction = positiveFinite(
    parameters.zone_ceiling_fraction,
    "document.parameters.zone_ceiling_fraction",
  );
  positiveFinite(
    parameters.occupied_ceiling_g,
    "document.parameters.occupied_ceiling_g",
  );
  positiveFinite(
    parameters.recovery_min_s,
    "document.parameters.recovery_min_s",
  );
  positiveFinite(
    parameters.cooldown_min_s,
    "document.parameters.cooldown_min_s",
  );
  if (critical <= hardMax) {
    throw new Error("document.parameters.hr_critical_bpm must exceed hr_hard_max_bpm");
  }
  if (zoneCeilingFraction > 1) {
    throw new Error("document.parameters.zone_ceiling_fraction must not exceed 1");
  }
  if (parameters.hr_max_formula !== "tanaka") {
    throw new Error("document.parameters.hr_max_formula must be tanaka");
  }
  if (hardMax < 100 || hardMax > 220 || critical < 100 || critical > 220) {
    throw new Error("heart-rate bounds must be in 100..220 bpm");
  }
  if (minRiderAge < 10 || minRiderAge > 100) {
    throw new Error("document.parameters.min_rider_age must be in 10..100 years");
  }
  const motion = objectMembers(
    parameters.motion_limits,
    MOTION_KEYS,
    "document.parameters.motion_limits",
  );
  const outputRate = positiveFinite(
    motion.output_rpm_per_s,
    "document.parameters.motion_limits.output_rpm_per_s",
  );
  const gRate = positiveFinite(
    motion.g_per_s,
    "document.parameters.motion_limits.g_per_s",
  );
  const minRun = positiveWhole(
    motion.min_run_motor_rpm,
    "document.parameters.motion_limits.min_run_motor_rpm",
  );
  const maxInterval = positiveFinite(
    motion.max_interval_s,
    "document.parameters.motion_limits.max_interval_s",
  );
  if (outputRate > 2 || gRate > 0.5 || minRun > 200 || maxInterval > 2) {
    throw new Error("document.parameters.motion_limits exceed console bounds");
  }

  return {
    version: document.version,
    issuedAt: document.issued_at,
    signedBy: document.signed_by,
    zoneCeilingFraction,
    minRiderAge,
    hrMaxFormula: parameters.hr_max_formula as HrMaxFormula,
  };
}

/** Compare three numeric version components in semantic version order. */
export function compareMedicalVersions(left: string, right: string): number {
  const leftParts = left.split(".").map(Number);
  const rightParts = right.split(".").map(Number);
  for (let index = 0; index < 3; index += 1) {
    const difference = (leftParts[index] ?? 0) - (rightParts[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

/** Classify a reported version, preserving absent and unsigned as distinct states. */
export function medicalParametersStatus(
  machineVersion: string | null | undefined,
  referenceVersion: string | null,
): MedicalParametersStatus {
  if (machineVersion === undefined) return "unknown";
  if (machineVersion === null) return "unsigned";
  if (referenceVersion === null) return "no_reference";
  const comparison = compareMedicalVersions(machineVersion, referenceVersion);
  if (comparison === 0) return "current";
  return comparison < 0 ? "obsolete" : "ahead";
}

function decodeBase64(value: string): Uint8Array<ArrayBuffer> | null {
  if (!/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)) {
    return null;
  }
  try {
    const decoded = atob(value);
    const bytes = Uint8Array.from(decoded, (character) => character.charCodeAt(0));
    return btoa(decoded) === value ? bytes : null;
  } catch {
    return null;
  }
}

function decodePem(publicKeyPem: string): Uint8Array<ArrayBuffer> | null {
  const match =
    /^-----BEGIN PUBLIC KEY-----\s*([A-Za-z0-9+/=\s]+)\s*-----END PUBLIC KEY-----$/.exec(
      publicKeyPem.trim(),
    );
  if (!match?.[1]) return null;
  return decodeBase64(match[1].replace(/\s/g, ""));
}

/** Verify an Ed25519 signature over the exact UTF-8 document bytes. */
export async function verifyMedicalParametersSignature(
  content: string,
  signatureBase64: string,
  publicKeyPem: string,
): Promise<boolean> {
  const signature = decodeBase64(signatureBase64);
  const publicKey = decodePem(publicKeyPem);
  if (!signature || signature.byteLength !== 64 || !publicKey) return false;
  try {
    const key = await crypto.subtle.importKey(
      "spki",
      publicKey,
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      signature,
      new TextEncoder().encode(content),
    );
  } catch {
    return false;
  }
}

/** Read the registered version with the greatest semantic version. */
export async function currentMedicalParameters(
  ctx: QueryCtx | MutationCtx,
): Promise<Doc<"medical_parameters_versions"> | null> {
  const rows = await ctx.db.query("medical_parameters_versions").collect();
  let current: Doc<"medical_parameters_versions"> | null = null;
  for (const row of rows) {
    if (
      current === null ||
      compareMedicalVersions(row.version, current.version) > 0
    ) {
      current = row;
    }
  }
  return current;
}
