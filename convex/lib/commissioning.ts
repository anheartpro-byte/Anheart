/**
 * The commissioning of a machine (ANH-115, contract 1.18, `machine_commissioning`
 * of contracts/machine-api.json).
 *
 * The console runs its commissioning checks at rest, the capsule empty, and
 * sends what it judged and read (`parseCommissioningReport`). The server
 * compares it with what the registry expects (`findingsOf`): the drive values
 * typed with the drive sheet, at a tenth, the BITalino's address and the
 * software version. A declared STO bridge, or any STO source but the drive's
 * input, is always a finding: such a machine is never recorded conforming.
 * The record is one canonical text (`commissioningPayload`), signed on the
 * catalogue's channel when the key is set, written once and never changed.
 *
 * No PDF: the site renders the record as a page to print, the browser saving
 * it as a PDF, like the session report and the logbook, and exports the
 * signed text itself.
 */
import { v, type Infer } from "convex/values";
import { z } from "zod";
import machineApi from "../../contracts/machine-api.json";
import { CONFIG_HASH } from "./machineConfig";

const SECTION = machineApi.machine_commissioning;

/** The `kind` of the signed text: a record is never read as a catalogue or a list. */
export const COMMISSIONING_KIND = SECTION.kind;

/** The drive values a sheet carries and a console reads back, in the contract's order. */
export const DRIVE_FIELDS = ["tfrHz", "hspHz", "lspHz", "accS", "decS"] as const;
export type DriveField = (typeof DRIVE_FIELDS)[number];

/** The keypad's range of each value typed with a sheet (`sheet_bounds`), for the server and the site's form. */
export const SHEET_BOUNDS: Readonly<Record<DriveField, { min: number; max: number }>> =
  SECTION.sheet_bounds;

export const driveParametersValidator = v.object({
  tfrHz: v.number(),
  hspHz: v.number(),
  lspHz: v.number(),
  accS: v.number(),
  decS: v.number(),
});
export type DriveParameters = Infer<typeof driveParametersValidator>;

export const COMMISSIONING_VERDICTS = ["conforming", "nonconforming"] as const;
export const commissioningVerdictValidator = v.union(
  v.literal("conforming"),
  v.literal("nonconforming"),
);
export type CommissioningVerdict = Infer<typeof commissioningVerdictValidator>;

export const commissioningFindingValidator = v.object({
  id: v.string(),
  expected: v.string(),
  read: v.string(),
});
export type CommissioningFinding = Infer<typeof commissioningFindingValidator>;

/** What a commissioning is compared with: the registry's, as it stood. */
export const commissioningReferenceValidator = v.object({
  sheet: v.object({
    // The file of the sheet the values were typed with: the record names which one.
    storageId: v.id("_storage"),
    version: v.string(),
    signedBy: v.string(),
    at: v.number(),
    parameters: driveParametersValidator,
  }),
  bitalinoAddress: v.union(v.string(), v.null()),
  expectedSoftwareVersion: v.union(v.string(), v.null()),
});
export type CommissioningReference = Infer<typeof commissioningReferenceValidator>;

/** The words of a refusal, nothing written (409 `commissioning_refused`). */
export const COMMISSIONING_REFUSALS = {
  noSheet:
    "Aucune fiche variateur avec ses valeurs au registre de cette machine : la joindre sur le site avant la mise en service",
  operator: "L'opérateur de la mise en service n'est pas un opérateur habilité de cette machine",
  attester:
    "L'auteur de l'attestation de l'arrêt d'urgence n'est pas un opérateur habilité de cette machine",
  changed: "Le registre de la machine a changé pendant l'enregistrement : relancer la mise en service",
  replayed:
    "Cette mise en service est déjà enregistrée : même machine, même fin des contrôles à la console",
} as const;

/** The controls a commissioning must carry, each green (`required_controls`). */
export const REQUIRED_CONTROLS: readonly string[] = SECTION.required_controls;

/** The latest date a JavaScript date holds (`max_date_ms`): a later one cannot be shown. */
export const MAX_DATE_MS: number = SECTION.max_date_ms;

/**
 * How far ahead of the server's clock a console may date the end of its checks
 * (`clock_skew_ms`). It sends them at once; its clock, when the time service
 * holds it, is within 5 s (the console's `CLOCK_MAX_ERROR`, judged by its
 * `clock` control, which the report carries). A minute covers that bound, the
 * link's latency and the server's own error, and refuses a date of the future
 * that only a wrong clock or a forgery writes.
 */
export const CLOCK_SKEW_MS: number = SECTION.clock_skew_ms;

/** A value as a whole number of tenths, or null when it is not one (50.05, NaN). */
export function tenthsOf(value: number): number | null {
  if (!Number.isFinite(value)) return null;
  const scaled = Math.round(value * 10);
  return Math.abs(value * 10 - scaled) < 1e-6 ? scaled : null;
}

/**
 * Why values typed with a drive sheet are refused, the field named; null when
 * they hold: whole tenths inside the keypad's ranges (`sheet_bounds`), and
 * LSP <= HSP <= tFr, as the console arms only on.
 */
export function sheetParametersRefusal(parameters: DriveParameters): DriveField | null {
  for (const field of DRIVE_FIELDS) {
    const value = parameters[field];
    const bound = SHEET_BOUNDS[field];
    if (tenthsOf(value) === null || value < bound.min || value > bound.max) return field;
  }
  if (parameters.hspHz > parameters.tfrHz) return "hspHz";
  if (parameters.lspHz > parameters.hspHz) return "lspHz";
  return null;
}

/**
 * What no text of a report holds: C0, DEL and C1 controls, the bidirectional
 * overrides and isolates, the line and paragraph separators, the zero-width
 * characters. A record is read by people: nothing in it may hide or reorder
 * what it shows.
 */
export const FORBIDDEN_CHARS =
  /[\u0000-\u001f\u007f-\u009f\u200b-\u200d\u2028\u2029\u202a-\u202e\u2060\u2066-\u2069\ufeff]/;

/** Text of 1 to `max` characters, none of `FORBIDDEN_CHARS`, no space at either end. */
function text(max: number) {
  return z
    .string()
    .min(1)
    .max(max)
    .refine((value) => value.trim() === value && !FORBIDDEN_CHARS.test(value));
}

const short = text(SECTION.max_text_chars);
const millis = z.number().int().nonnegative().max(MAX_DATE_MS);
const read = z
  .number()
  .min(0)
  .max(SECTION.drive_read_max)
  .refine((value) => tenthsOf(value) !== null);

const reportSchema = z.strictObject({
  ranAt: millis,
  operator: short,
  attestation: z.strictObject({
    operator: short,
    statement: text(SECTION.max_statement_chars),
    at: millis,
  }),
  stoSource: z.enum(
    machineApi.health_state.sto_sources as [string, ...string[]],
  ),
  controls: z
    .array(
      z.strictObject({
        id: z.string().regex(new RegExp(`^[a-z][a-z0-9_]{0,${SECTION.max_control_id_chars - 1}}$`)),
        state: z.enum(SECTION.control_states as [string, ...string[]]),
        detail: text(SECTION.max_detail_chars),
      }),
    )
    .min(1)
    .max(SECTION.max_controls)
    .refine((controls) => new Set(controls.map((c) => c.id)).size === controls.length),
  drive: z
    .strictObject({ tfrHz: read, hspHz: read, lspHz: read, accS: read, decS: read })
    .nullable(),
  bitalinoAddress: short.nullable(),
  softwareVersion: short,
  medicalParametersVersion: short.nullable(),
  configHash: z.string().regex(CONFIG_HASH).nullable(),
});
export type CommissioningReport = z.infer<typeof reportSchema>;

/**
 * The report a console sent, or null for a body that is not one: its
 * attestation given before the end of its checks, and that end no later than
 * `now`, the server's clock, plus `CLOCK_SKEW_MS`.
 */
export function parseCommissioningReport(
  body: unknown,
  now: number,
): CommissioningReport | null {
  const parsed = reportSchema.safeParse(body);
  if (!parsed.success) return null;
  const report = parsed.data;
  return report.attestation.at <= report.ranAt && report.ranAt <= now + CLOCK_SKEW_MS
    ? report
    : null;
}

const FINDING_OF: Record<DriveField, string> = {
  tfrHz: "drive_tfr",
  hspHz: "drive_hsp",
  lspHz: "drive_lsp",
  accS: "drive_acc",
  decS: "drive_dec",
};
const UNIT_OF: Record<DriveField, string> = {
  tfrHz: "Hz",
  hspHz: "Hz",
  lspHz: "Hz",
  accS: "s",
  decS: "s",
};
const NOT_IN_REGISTRY = "non renseignée au registre";
const NOT_READ = "non lue";
/** The STO source of a machine whose emergency stop opens its drive's STO input. */
const WIRED_STO = "drive_input";

function shown(field: DriveField, value: number): string {
  return `${value.toFixed(1)} ${UNIT_OF[field]}`;
}

/** An address as compared: no space at either end, lower case. */
function addressOf(address: string | null): string | null {
  return address === null ? null : address.trim().toLowerCase();
}

/**
 * Every difference between what the console judged and read and what the
 * registry expects, in a fixed order: the STO source, the console's controls
 * (a required one not green, any other red, then each required one missing),
 * the drive values, the BITalino, the software version.
 */
export function findingsOf(
  report: CommissioningReport,
  reference: CommissioningReference,
): CommissioningFinding[] {
  const findings: CommissioningFinding[] = [];
  if (report.stoSource !== WIRED_STO) {
    findings.push({
      id: report.stoSource === "bridged_declared" ? "sto_bridged" : "sto_source",
      expected: WIRED_STO,
      read: report.stoSource,
    });
  }
  // A required control is green; any other is a finding only red.
  for (const control of report.controls) {
    const required = REQUIRED_CONTROLS.includes(control.id);
    if (control.state === "block" || (required && control.state !== "ok"))
      findings.push({ id: `control:${control.id}`, expected: "ok", read: control.detail });
  }
  const sent = new Set(report.controls.map((control) => control.id));
  for (const id of REQUIRED_CONTROLS) {
    if (!sent.has(id))
      findings.push({ id: `control_missing:${id}`, expected: "ok", read: "absent du rapport" });
  }
  const drive = report.drive;
  if (drive === null) {
    findings.push({ id: "drive_unread", expected: "lus", read: "non lus" });
  } else {
    for (const field of DRIVE_FIELDS) {
      const expected = reference.sheet.parameters[field];
      if (tenthsOf(drive[field]) !== tenthsOf(expected))
        findings.push({
          id: FINDING_OF[field],
          expected: shown(field, expected),
          read: shown(field, drive[field]),
        });
    }
  }
  const address = addressOf(reference.bitalinoAddress);
  const readAddress = addressOf(report.bitalinoAddress);
  if (address === null || address !== readAddress)
    findings.push({
      id: "bitalino_address",
      expected: address ?? NOT_IN_REGISTRY,
      read: readAddress ?? NOT_READ,
    });
  const version = reference.expectedSoftwareVersion;
  if (version === null || version !== report.softwareVersion)
    findings.push({
      id: "software_version",
      expected: version ?? NOT_IN_REGISTRY,
      read: report.softwareVersion,
    });
  return findings;
}

/** Conforming only without a single finding. */
export function verdictOf(findings: readonly CommissioningFinding[]): CommissioningVerdict {
  return findings.length === 0 ? "conforming" : "nonconforming";
}

/**
 * The record's text, its keys in a fixed order: its bytes are what is signed,
 * and what the site exports with the signature.
 */
export function commissioningPayload(record: {
  machineId: string;
  recordedAt: number;
  verdict: CommissioningVerdict;
  findings: readonly CommissioningFinding[];
  reference: CommissioningReference;
  report: CommissioningReport;
}): string {
  const { report } = record;
  const drive = report.drive;
  return JSON.stringify({
    kind: COMMISSIONING_KIND,
    machineId: record.machineId,
    recordedAt: record.recordedAt,
    verdict: record.verdict,
    findings: record.findings.map((f) => ({ id: f.id, expected: f.expected, read: f.read })),
    reference: orderedReference(record.reference),
    report: {
      ranAt: report.ranAt,
      operator: report.operator,
      attestation: {
        operator: report.attestation.operator,
        statement: report.attestation.statement,
        at: report.attestation.at,
      },
      stoSource: report.stoSource,
      controls: report.controls.map((c) => ({ id: c.id, state: c.state, detail: c.detail })),
      drive: drive === null ? null : parametersOf(drive),
      bitalinoAddress: report.bitalinoAddress,
      softwareVersion: report.softwareVersion,
      medicalParametersVersion: report.medicalParametersVersion,
      configHash: report.configHash,
    },
  });
}

function orderedReference(reference: CommissioningReference): CommissioningReference {
  return {
    sheet: {
      storageId: reference.sheet.storageId,
      version: reference.sheet.version,
      signedBy: reference.sheet.signedBy,
      at: reference.sheet.at,
      parameters: parametersOf(reference.sheet.parameters),
    },
    bitalinoAddress: reference.bitalinoAddress,
    expectedSoftwareVersion: reference.expectedSoftwareVersion,
  };
}

/** Whether two references are the same registry state, whatever their keys' order. */
export function sameReference(a: CommissioningReference, b: CommissioningReference): boolean {
  return JSON.stringify(orderedReference(a)) === JSON.stringify(orderedReference(b));
}

function parametersOf(parameters: DriveParameters): DriveParameters {
  return {
    tfrHz: parameters.tfrHz,
    hspHz: parameters.hspHz,
    lspHz: parameters.lspHz,
    accS: parameters.accS,
    decS: parameters.decS,
  };
}
