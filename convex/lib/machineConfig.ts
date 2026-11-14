/**
 * The machine configuration held in Convex and applied at rest by the console
 * (ANH-141): its keys, its validation, its canonical text and the signed text
 * the machines fetch.
 *
 * The keys, their family, type, bounds and default are
 * `contracts/machine-config.json`, the one list both sides read.
 * `machineConfigProblems` judges values as the console does
 * (`review`, `raspberry-pi/src/machine_config.py`, which hands them to
 * `load_local_config`): same problems, same order, same words, held equal by
 * `contracts/machine-config-cases.json` (`convex/machineConfigs.test.ts` and
 * `raspberry-pi/tests/test_machine_config_parity.py`). A key refused for its
 * type is left unset for the rest of the judgement, as the console does. The
 * console's local keys (its link, its page's host and token, its safety relay
 * wiring) are not Convex's: they are taken valid here, and the console judges
 * them again with the values.
 *
 * The console stays the authority: what is approved here is a proposal it
 * revalidates, and refuses whatever it cannot.
 */
import { v } from "convex/values";
import machineConfig from "../../contracts/machine-config.json";
import machineApi from "../../contracts/machine-api.json";
import type { Doc } from "../_generated/dataModel";
import type { QueryCtx } from "../_generated/server";
import type { MachineValidationLevel } from "./releaseValidation";
import { configurationLockVerdict, type ConfigurationKey } from "./machineRegistry";
import { PYTHON_WHITESPACE } from "./programmes";

/** A value of the configuration: `null` is a nullable key left unset. */
export type ConfigValue = string | number | boolean | null | string[];
export type ConfigValues = Record<string, ConfigValue>;
/** A value as Convex stores and takes it: the shapes the contract's types use. */
export const configValueValidator = v.union(
  v.string(),
  v.number(),
  v.boolean(),
  v.null(),
  v.array(v.string()),
);
export const configValuesValidator = v.record(v.string(), configValueValidator);

/**
 * `draft` (proposed, not approved), `approved` (what the machine is sent),
 * `applied` (the machine said it applies it), `rejected` (withdrawn, replaced
 * before it applied, or refused by the machine).
 */
export const machineConfigStatusValidator = v.union(
  v.literal("draft"),
  v.literal("approved"),
  v.literal("applied"),
  v.literal("rejected"),
);

/** One revision of a machine's configuration (ANH-141). Its values are never rewritten. */
export const machineConfigFields = {
  organizationId: v.optional(v.id("organizations")),
  machineId: v.id("machines"),
  /** 1 for the machine's first, plus one for each proposal. */
  revision: v.number(),
  values: configValuesValidator,
  /** `canonicalConfig(values)`: the text the machine is sent, and hashed. */
  canonical: v.string(),
  /** SHA-256 of `canonical`, lowercase hexadecimal. */
  hash: v.string(),
  status: machineConfigStatusValidator,
  /** The keys that differ from the revision applied when it was proposed (all of them without one). */
  changedKeys: v.array(v.string()),
  proposedBy: v.id("users"),
  proposedByRole: v.union(v.literal("admin"), v.literal("org_admin")),
  proposedAt: v.number(),
  /**
   * Who validated it. When a safety key changes from the revision applied:
   * the author and another Anheart admin, two distinct accounts. Otherwise
   * the one approver, who may be the author.
   */
  approvedBy: v.optional(v.array(v.id("users"))),
  approvedAt: v.optional(v.number()),
  appliedAt: v.optional(v.number()),
  appliedHash: v.optional(v.string()),
  rejectedAt: v.optional(v.number()),
  /** Absent when the machine refused it or a later approval replaced it. */
  rejectedBy: v.optional(v.id("users")),
  rejectReason: v.optional(v.string()),
};

/** One refused key and what the console says of it (`ConfigProblem` of local_config.py). */
export type ConfigProblem = { key: string; detail: string };

export type ConfigFamily = "exploitation" | "safety";
export type ConfigType =
  | "text"
  | "token"
  | "enum"
  | "enum_list"
  | "integer"
  | "number"
  | "boolean";

/** One key of the contract. */
export type ConfigKeySpec = {
  family: ConfigFamily;
  env: string | null;
  type: ConfigType;
  nullable: boolean;
  default: ConfigValue;
  max_chars?: number;
  values?: string[];
  min?: number;
  max?: number;
  min_exclusive?: number;
  not?: number[];
  min_key?: string;
  required_value?: string;
  requires_level?: MachineValidationLevel;
};

/** The keys, in the contract's order. */
export const CONFIG_KEYS = machineConfig.keys as unknown as Readonly<
  Record<string, ConfigKeySpec>
>;
export const CONFIG_KEY_NAMES: readonly string[] = Object.keys(CONFIG_KEYS);
/** The keys whose change needs two admins and an operator at the console. */
export const SAFETY_KEYS: readonly string[] = CONFIG_KEY_NAMES.filter(
  (key) => CONFIG_KEYS[key].family === "safety",
);
/** What an organisation admin may change: the machine's name and its place. */
export const ORGANIZATION_KEYS: readonly string[] = ["name", "location"];

const SECTION = machineApi.machine_config;
/** What a signed configuration says it is: never a catalogue nor an operator list. */
export const MACHINE_CONFIG_KIND: string = SECTION.kind;
/** The largest signed text a console is sent (`max_payload_bytes`). */
export const MAX_MACHINE_CONFIG_BYTES: number = SECTION.max_payload_bytes;
/** The longest refusal a console reports, in code points (`max_reason_chars`). */
export const MAX_REFUSAL_CHARS: number = SECTION.max_reason_chars;
/** A configuration's hash: SHA-256 in lowercase hexadecimal (`hash_pattern`). */
export const CONFIG_HASH = new RegExp(SECTION.hash_pattern);

export const REQUIRED = "obligatoire";
export const MISSING = "cle absente";
export const UNKNOWN = "cle inconnue : la console ne la revalide pas";

const TYPE_WORDS: Record<ConfigType, (spec: ConfigKeySpec) => string> = {
  text: (spec) =>
    `attendu un texte de 1 a ${spec.max_chars} caracteres, sans caractere de controle ni espace en tete ou en fin`,
  token: (spec) =>
    `attendu 1 a ${spec.max_chars} caracteres ASCII imprimables, sans espace, guillemet, apostrophe ni barre oblique inverse`,
  enum: (spec) => `attendu l'une des valeurs : ${(spec.values ?? []).join(", ")}`,
  enum_list: (spec) =>
    `attendu une liste non vide, sans doublon, parmi : ${(spec.values ?? []).join(", ")}`,
  integer: () => "attendu un nombre entier",
  number: () => "attendu un nombre fini",
  boolean: () => "attendu true ou false",
};

/** Printable ASCII without space, quote, apostrophe or backslash: a port, an address. */
const TOKEN = /^[!#-&(-[\]-~]+$/;
/** A control character, or a surrogate not paired (with `u`, a pair is one code point). */
const CONTROL_OR_LONE_SURROGATE = /[\x00-\x1f\x7f-\x9f\uD800-\uDFFF]/u;

function plainText(raw: string): boolean {
  const characters = Array.from(raw);
  const edge = (character: string | undefined) =>
    character !== undefined && PYTHON_WHITESPACE.has(character.charCodeAt(0));
  return (
    !edge(characters[0]) &&
    !edge(characters[characters.length - 1]) &&
    !CONTROL_OR_LONE_SURROGATE.test(raw)
  );
}

/** `raw` as a value of `spec`, or undefined when it is not one (`read_value`). */
function readValue(spec: ConfigKeySpec, raw: ConfigValue): ConfigValue | undefined {
  const values = spec.values ?? [];
  switch (spec.type) {
    case "text":
    case "token":
    case "enum": {
      if (typeof raw !== "string") return undefined;
      if (spec.type === "enum") return values.includes(raw) ? raw : undefined;
      const length = Array.from(raw).length;
      const fits = length > 0 && length <= (spec.max_chars ?? 0);
      if (spec.type === "token") return fits && TOKEN.test(raw) ? raw : undefined;
      return fits && plainText(raw) ? raw : undefined;
    }
    case "enum_list":
      return Array.isArray(raw) &&
        raw.length > 0 &&
        raw.every((name) => values.includes(name)) &&
        new Set(raw).size === raw.length
        ? raw
        : undefined;
    case "integer":
      // -0 is the 0 JSON writes.
      return typeof raw === "number" && Number.isSafeInteger(raw) ? raw + 0 : undefined;
    case "number":
      return typeof raw === "number" && Number.isFinite(raw) ? raw + 0 : undefined;
    case "boolean":
      return typeof raw === "boolean" ? raw : undefined;
  }
}

const own = (object: object, key: string) => Object.prototype.hasOwnProperty.call(object, key);

/** Every key read, and the problems of those that could not be (`read_values`). */
export function readConfigValues(document: Record<string, ConfigValue>): {
  values: Partial<ConfigValues>;
  problems: ConfigProblem[];
} {
  const values: Partial<ConfigValues> = {};
  const problems: ConfigProblem[] = [];
  for (const key of CONFIG_KEY_NAMES) {
    const spec = CONFIG_KEYS[key];
    if (!own(document, key)) {
      problems.push({ key, detail: MISSING });
      continue;
    }
    const raw = document[key];
    if (raw === null) {
      if (spec.nullable) values[key] = null;
      else problems.push({ key, detail: REQUIRED });
      continue;
    }
    const value = readValue(spec, raw);
    if (value === undefined) problems.push({ key, detail: TYPE_WORDS[spec.type](spec) });
    else values[key] = value;
  }
  for (const key of Object.keys(document)
    .filter((name) => !own(CONFIG_KEYS, name))
    .sort())
    problems.push({ key, detail: UNKNOWN });
  return { values, problems };
}

/**
 * A float as Python writes it (`repr`): the console's words carry its numbers
 * so. The same shortest digits as JavaScript, placed by Python's rule: plain
 * from 1e-4 to below 1e16, with a `.0` when whole; otherwise `1e-05`, `1e+16`.
 */
export function pythonFloat(value: number): string {
  const [mantissa, exponentText] = value.toExponential().split("e");
  const exponent = Number(exponentText);
  const sign = mantissa.startsWith("-") ? "-" : "";
  const digits = mantissa.replace("-", "").replace(".", "");
  if (exponent < -4 || exponent >= 16) {
    const shown = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const magnitude = String(Math.abs(exponent)).padStart(2, "0");
    return `${sign}${shown}e${exponent < 0 ? "-" : "+"}${magnitude}`;
  }
  if (exponent < 0) return `${sign}0.${"0".repeat(-exponent - 1)}${digits}`;
  const whole = digits.slice(0, exponent + 1).padEnd(exponent + 1, "0");
  return `${sign}${whole}.${digits.slice(exponent + 1) || "0"}`;
}

/** The value of `key` the console would use: read, else its default (an unset key). */
function effective<T extends ConfigValue>(values: Partial<ConfigValues>, key: string): T {
  const value = values[key];
  return (value === undefined || value === null ? CONFIG_KEYS[key].default : value) as T;
}

const bound = (key: string, field: "min" | "max") => CONFIG_KEYS[key][field] as number;

/** `build_drive_link`: the first refusal of the link, read only for the real drive. */
function driveLinkProblem(values: Partial<ConfigValues>): string | null {
  const slave = effective<number>(values, "MOTOR_SLAVE_ID");
  const timeout = effective<number>(values, "MODBUS_TIMEOUT_S");
  const offset = effective<number>(values, "MOTOR_REG_OFFSET");
  if (slave < bound("MOTOR_SLAVE_ID", "min") || slave > bound("MOTOR_SLAVE_ID", "max"))
    return (
      `Modbus slave address ${slave} is neither in 1..247 nor Schneider's point-to-point ` +
      "address 248; address 0 is the broadcast address, which commands every drive on the " +
      "bus and cannot be verified"
    );
  if (timeout <= 0)
    return (
      `serial timeout ${pythonFloat(timeout)} s must be positive; a zero timeout makes every ` +
      "read return nothing, which this driver would report as a dead drive"
    );
  const low = bound("MOTOR_REG_OFFSET", "min");
  const high = bound("MOTOR_REG_OFFSET", "max");
  if (offset < low || offset > high)
    return (
      `register offset ${offset} is outside ${low}..${high}; the only legitimate offset is ` +
      "a 0/1-based indexing difference, and a larger one would address an unrelated drive " +
      "parameter"
    );
  return null;
}

/** `bitalino_address`: the address checked against what the ECG source needs. */
function addressProblem(source: string, address: string): string | null {
  if (source === "rfcomm")
    return /^rfcomm:[0-9a-f]{2}(?:-[0-9a-f]{2}){5}$/i.test(address)
      ? null
      : `'${address}' : ECG_SOURCE=rfcomm attend rfcomm:XX-XX-XX-XX-XX-XX`;
  return address !== "" && !address.toLowerCase().startsWith("rfcomm:")
    ? null
    : `'${address}' : ECG_SOURCE=serial attend un port serie (/dev/rfcomm0, COM4) ou une adresse MAC`;
}

/**
 * What `load_local_config` refuses of values read, in its order. A key absent
 * from `values` was refused for its type: unset here, and what the console
 * says of it unset is left out, as `review` leaves it out.
 */
function consoleProblems(values: Partial<ConfigValues>): ConfigProblem[] {
  const problems: ConfigProblem[] = [];
  const add = (key: string, detail: string | null) => {
    if (detail !== null) problems.push({ key, detail });
  };
  if (values.MOTOR_BACKEND === "serial") add("MOTOR_*", driveLinkProblem(values));
  const source = values.ECG_SOURCE;
  if (source === "rfcomm" || source === "serial")
    add("BITALINO_ADDRESS", addressProblem(source, (values.BITALINO_ADDRESS as string | null) ?? ""));
  const maxRadius = bound("ARM_RADIUS_M", "max");
  const arm = values.ARM_RADIUS_M as number | undefined;
  const armRefused = arm !== undefined && (arm <= 0 || arm > maxRadius);
  if (armRefused)
    add("ARM_RADIUS_M", `${pythonFloat(arm)} m hors de ]0, ${pythonFloat(maxRadius)}]`);
  const ratio = effective<number>(values, "GEAR_RATIO");
  if (ratio <= 0) add("GEAR_RATIO", `${pythonFloat(ratio)} doit etre positif`);
  const rpm = effective<number>(values, "MOTOR_MAX_RPM");
  const plate = bound("MOTOR_MAX_RPM", "max");
  if (rpm < bound("MOTOR_MAX_RPM", "min") || rpm > plate)
    add("MOTOR_MAX_RPM", `${rpm} tr/min hors de 0..${plate} (plaque signaletique)`);
  const required = CONFIG_KEYS.SENSORS.required_value ?? "";
  if (!effective<string[]>(values, "SENSORS").includes(required))
    add("SENSORS", `${required} obligatoire : la frequence cardiaque pilote le moteur`);
  const leg = values.LEG_TIP_RADIUS_M as number | null | undefined;
  const low = arm === undefined || armRefused ? 0 : arm;
  if (leg !== undefined && leg !== null && (leg < low || leg <= 0 || leg > maxRadius))
    add(
      "LEG_TIP_RADIUS_M",
      `${pythonFloat(leg)} m hors de [ARM_RADIUS_M, ${pythonFloat(maxRadius)}] : la pointe ` +
        "du pied est au moins aussi loin de l'axe que le point de reference",
    );
  const port = effective<number>(values, "UI_PORT");
  if ((CONFIG_KEYS.UI_PORT.not ?? []).includes(port))
    add("UI_PORT", `${port} est le port de scripts/bench_console.py`);
  else if (port < bound("UI_PORT", "min") || port > bound("UI_PORT", "max"))
    add(
      "UI_HOST/UI_PORT/DEVICE_TOKEN",
      `port must be ${bound("UI_PORT", "min")}..${bound("UI_PORT", "max")}, got ${port}`,
    );
  const days = effective<number>(values, "RECORD_LOCAL_RETENTION_DAYS");
  const most = bound("RECORD_LOCAL_RETENTION_DAYS", "max");
  if (days < bound("RECORD_LOCAL_RETENTION_DAYS", "min") || days > most)
    add("RECORD_LOCAL_RETENTION_DAYS", `${days} jours hors de 0..${most}`);
  return problems;
}

/**
 * Every problem of `document` for a machine validated to `level`, as the
 * console states them, in its order; empty when it would accept it. The two
 * locks (EX-5) are refused above the registry's level with the registry's
 * own words (`configurationLockVerdict`).
 */
export function machineConfigProblems(
  document: Record<string, ConfigValue>,
  level: MachineValidationLevel,
): ConfigProblem[] {
  const { values, problems } = readConfigValues(document);
  const locks = CONFIG_KEY_NAMES.flatMap((key) => {
    const value = values[key];
    if (CONFIG_KEYS[key].requires_level === undefined || typeof value !== "boolean") return [];
    const verdict = configurationLockVerdict(level, key as ConfigurationKey, value);
    return verdict.message === null ? [] : [{ key, detail: verdict.message }];
  });
  return [...problems, ...consoleProblems(values), ...locks];
}

/** The text a configuration travels as: its keys sorted, no space; the bytes hashed. */
export function canonicalConfig(values: ConfigValues): string {
  return JSON.stringify(
    Object.fromEntries(Object.keys(values).sort().map((key) => [key, values[key]])),
  );
}

/** The SHA-256 of `text`'s UTF-8 bytes, in lowercase hexadecimal: a configuration's hash. */
export async function configHash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** The keys whose value differs from `base`; every key when there is no base. */
export function changedKeys(base: ConfigValues | null, values: ConfigValues): string[] {
  return CONFIG_KEY_NAMES.filter(
    (key) => base === null || JSON.stringify(base[key]) !== JSON.stringify(values[key]),
  );
}

/** The defaults of the contract, for a machine that has no configuration yet. */
export function contractDefaults(): ConfigValues {
  return Object.fromEntries(CONFIG_KEY_NAMES.map((key) => [key, CONFIG_KEYS[key].default]));
}

/**
 * The text the machines are sent signed (`machine_config` of
 * contracts/machine-api.json): the revision in force (approved, or applied),
 * its canonical values as text and their hash, and the level the registry
 * has validated the machine to, which the console checks the locks against
 * again. `revision` null: nothing approved yet. Built once and never
 * serialised again: its bytes are what is signed.
 */
export function machineConfigPayload(entry: {
  machineId: string;
  issuedAt: number;
  validationLevel: MachineValidationLevel;
  revision: { revision: number; hash: string; values: string } | null;
}): string {
  return JSON.stringify({
    kind: MACHINE_CONFIG_KIND,
    machineId: entry.machineId,
    issuedAt: entry.issuedAt,
    validationLevel: entry.validationLevel,
    revision: entry.revision?.revision ?? null,
    hash: entry.revision?.hash ?? null,
    values: entry.revision?.values ?? null,
  });
}

export type ConfigurationStatus = "unavailable" | "pending" | "applied" | "mismatch";

/**
 * What the machine page and the list say of a machine's configuration:
 * `unavailable` with nothing approved, `pending` while the revision in force
 * is approved and not yet applied, `applied` once applied and reported by the
 * heartbeat's `config_hash`, `mismatch` when the heartbeat reports another
 * hash than the applied revision's ("configuration appliquée ≠ approuvée").
 */
export function configurationStatus(
  target: { status: "approved" | "applied"; hash: string } | null,
  reported: string | null | undefined,
): ConfigurationStatus {
  if (target === null) return "unavailable";
  if (target.status === "approved") return "pending";
  return reported === target.hash ? "applied" : "mismatch";
}

/** `configurationStatus` of `machine`, from its latest revision approved or applied. */
export async function configurationStatusOf(
  ctx: Pick<QueryCtx, "db">,
  machine: Pick<Doc<"machines">, "_id" | "configHash">,
): Promise<ConfigurationStatus> {
  const revisions = ctx.db
    .query("machine_configs")
    .withIndex("by_machine_and_revision", (q) => q.eq("machineId", machine._id))
    .order("desc");
  for await (const row of revisions) {
    if (row.status === "approved" || row.status === "applied")
      return configurationStatus({ status: row.status, hash: row.hash }, machine.configHash);
  }
  return "unavailable";
}
