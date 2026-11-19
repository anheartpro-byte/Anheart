/**
 * The commissioning records of the machines (ANH-115, contract 1.18).
 *
 * `POST /api/machine/commissioning` (convex/http.ts) reads what the console
 * judged and read, looks up what the registry expects (`reference`), compares
 * and signs (convex/lib/commissioning.ts), then writes the record
 * (`record`), which looks the registry up again in its own transaction: a
 * sheet replaced, or an operator disabled, in between refuses it, nothing
 * written. A record is never changed. Its line in the machine's logbook
 * (`commissioning`) names no person; the record keeps who ran it and who
 * attested the emergency stop as user ids, their names resolved only for
 * whoever reads the registry (`listCommissionings`, `getCommissioning`).
 *
 * A record changes nothing else: the machine's validation level stays what
 * it is, raised only by a review of the registry (ANH-147).
 */
import { v } from "convex/values";
import {
  internalMutation,
  internalQuery,
  query,
  type QueryCtx,
} from "./_generated/server";
import type { Doc, Id } from "./_generated/dataModel";
import { audit, machineActor } from "./lib/audit";
import {
  COMMISSIONING_REFUSALS,
  commissioningFindingValidator,
  commissioningReferenceValidator,
  commissioningVerdictValidator,
  driveParametersValidator,
  sameReference,
  type CommissioningFinding,
  type CommissioningReference,
  type CommissioningVerdict,
} from "./lib/commissioning";
import { LOGBOOK_DETAILS_MAX, writeLogbook } from "./lib/logbook";
import { onOperatorList } from "./consoleAccess";
import { readableMachine } from "./machineRegistry";

/** Records listed for one machine, most recent first, at most. */
export const COMMISSIONING_LIST_LIMIT = 50;

const refusedValidator = v.object({ status: v.literal("refused"), message: v.string() });

type Refused = { status: "refused"; message: string };
type Found = {
  status: "found";
  machine: Doc<"machines">;
  operator: Id<"users">;
  attestedBy: Id<"users">;
  reference: CommissioningReference;
};

function refused(message: string): Refused {
  return { status: "refused", message };
}

/**
 * What the registry expects of a commissioning of this machine, run by
 * `operator` with the attestation of `attestedBy`; why none can be recorded
 * otherwise: no sheet with its values, either of them not on the machine's
 * operator list now, or a record of the machine already ending at `ranAt`
 * (a report sent again).
 */
async function lookUp(
  ctx: QueryCtx,
  machineId: Id<"machines">,
  operator: string,
  attestedBy: string,
  ranAt: number,
): Promise<Refused | Found> {
  const machine = await ctx.db.get(machineId);
  const registry = await ctx.db
    .query("machine_registry")
    .withIndex("by_machine", (q) => q.eq("machineId", machineId))
    .unique();
  const sheet = registry?.driveParameterSheet;
  const parameters = sheet?.parameters;
  if (machine === null || machine.isDeleted || registry === null || !sheet || !parameters)
    return refused(COMMISSIONING_REFUSALS.noSheet);
  const operatorId = ctx.db.normalizeId("users", operator);
  if (operatorId === null || !(await onOperatorList(ctx, machine, operatorId)))
    return refused(COMMISSIONING_REFUSALS.operator);
  const attesterId = ctx.db.normalizeId("users", attestedBy);
  if (attesterId === null || !(await onOperatorList(ctx, machine, attesterId)))
    return refused(COMMISSIONING_REFUSALS.attester);
  const replayed = await ctx.db
    .query("machine_commissioning")
    .withIndex("by_machine_and_ran_at", (q) => q.eq("machineId", machineId).eq("ranAt", ranAt))
    .first();
  if (replayed !== null) return refused(COMMISSIONING_REFUSALS.replayed);
  return {
    status: "found",
    machine,
    operator: operatorId,
    attestedBy: attesterId,
    reference: {
      sheet: {
        storageId: sheet.storageId,
        version: sheet.version,
        signedBy: sheet.signedBy,
        at: sheet.at,
        parameters,
      },
      bitalinoAddress: registry.bitalinoAddress ?? null,
      expectedSoftwareVersion: registry.expectedSoftwareVersion ?? null,
    },
  };
}

/** What a commissioning of this machine is compared with, or why it is refused. */
export const reference = internalQuery({
  args: {
    machineId: v.id("machines"),
    operator: v.string(),
    attestedBy: v.string(),
    ranAt: v.number(),
  },
  returns: v.union(
    refusedValidator,
    v.object({ status: v.literal("found"), reference: commissioningReferenceValidator }),
  ),
  handler: async (ctx, args) => {
    const found = await lookUp(ctx, args.machineId, args.operator, args.attestedBy, args.ranAt);
    return found.status === "refused"
      ? found
      : { status: "found" as const, reference: found.reference };
  },
});

function summaryOf(verdict: CommissioningVerdict, findings: number, sheet: string): string {
  return verdict === "conforming"
    ? `mise en service conforme, fiche variateur ${sheet}`
    : `mise en service non conforme : ${findings} écart(s), fiche variateur ${sheet}`;
}

function detailsOf(findings: readonly CommissioningFinding[]): string | undefined {
  if (findings.length === 0) return undefined;
  return findings
    .map((f) => `${f.id} : attendu ${f.expected}, lu ${f.read}`)
    .join("\n")
    .slice(0, LOGBOOK_DETAILS_MAX);
}

/**
 * Write a commissioning: the record, its line in the logbook and the audit.
 * Refused, nothing written, when the registry no longer is what the record
 * was compared with.
 */
export const record = internalMutation({
  args: {
    machineId: v.id("machines"),
    operator: v.string(),
    attestedBy: v.string(),
    ranAt: v.number(),
    reference: commissioningReferenceValidator,
    recordedAt: v.number(),
    verdict: commissioningVerdictValidator,
    findings: v.array(commissioningFindingValidator),
    payload: v.string(),
    signature: v.union(v.string(), v.null()),
    softwareVersion: v.string(),
  },
  returns: v.union(
    refusedValidator,
    v.object({ status: v.literal("recorded"), recordId: v.id("machine_commissioning") }),
  ),
  handler: async (ctx, args) => {
    const found = await lookUp(ctx, args.machineId, args.operator, args.attestedBy, args.ranAt);
    if (found.status === "refused") return found;
    if (!sameReference(found.reference, args.reference))
      return refused(COMMISSIONING_REFUSALS.changed);
    const { machine } = found;
    const recordId = await ctx.db.insert("machine_commissioning", {
      machineId: machine._id,
      organizationId: machine.organizationId,
      recordedAt: args.recordedAt,
      ranAt: args.ranAt,
      operator: found.operator,
      attestedBy: found.attestedBy,
      verdict: args.verdict,
      findings: args.findings,
      payload: args.payload,
      signature: args.signature,
    });
    await writeLogbook(ctx, machine, {
      kind: "commissioning",
      at: args.recordedAt,
      summary: summaryOf(args.verdict, args.findings.length, found.reference.sheet.version),
      details: detailsOf(args.findings),
      refs: { softwareVersion: args.softwareVersion },
    });
    await audit(ctx, {
      source: "machine",
      actor: machineActor(machine._id),
      organizationId: machine.organizationId,
      action: "commissioning.recorded",
      targetId: machine._id,
    });
    return { status: "recorded" as const, recordId };
  },
});

/** A machine's commissioning records, most recent first; empty for whoever does not read its registry. */
export const listCommissionings = query({
  args: { machineId: v.id("machines") },
  returns: v.array(
    v.object({
      recordId: v.id("machine_commissioning"),
      recordedAt: v.number(),
      verdict: commissioningVerdictValidator,
      findings: v.number(),
      signed: v.boolean(),
    }),
  ),
  handler: async (ctx, args) => {
    if ((await readableMachine(ctx, args.machineId)) === null) return [];
    const rows = await ctx.db
      .query("machine_commissioning")
      .withIndex("by_machine_and_recorded_at", (q) => q.eq("machineId", args.machineId))
      .order("desc")
      .take(COMMISSIONING_LIST_LIMIT);
    return rows.map((row) => ({
      recordId: row._id,
      recordedAt: row.recordedAt,
      verdict: row.verdict,
      findings: row.findings.length,
      signed: row.signature !== null,
    }));
  },
});

const personValidator = v.object({
  userId: v.id("users"),
  firstName: v.optional(v.string()),
  lastName: v.optional(v.string()),
});

/** The parts of the signed text the page shows, read back from it. */
type RecordText = {
  reference: CommissioningReference;
  report: {
    ranAt: number;
    attestation: { statement: string; at: number };
    stoSource: string;
    controls: { id: string; state: string; detail: string }[];
    drive: CommissioningReference["sheet"]["parameters"] | null;
    bitalinoAddress: string | null;
    softwareVersion: string;
    medicalParametersVersion: string | null;
    configHash: string | null;
  };
};

/**
 * One record, for its page and its export: what was compared, what the
 * console judged and read, who ran it and who attested, by name, and the
 * signed text with its signature. Null for whoever does not read the
 * machine's registry.
 */
export const getCommissioning = query({
  args: { recordId: v.id("machine_commissioning") },
  returns: v.union(
    v.null(),
    v.object({
      recordId: v.id("machine_commissioning"),
      machineId: v.id("machines"),
      recordedAt: v.number(),
      verdict: commissioningVerdictValidator,
      findings: v.array(commissioningFindingValidator),
      operator: personValidator,
      attestedBy: personValidator,
      reference: commissioningReferenceValidator,
      report: v.object({
        ranAt: v.number(),
        statement: v.string(),
        attestedAt: v.number(),
        stoSource: v.string(),
        controls: v.array(v.object({ id: v.string(), state: v.string(), detail: v.string() })),
        drive: v.union(driveParametersValidator, v.null()),
        bitalinoAddress: v.union(v.string(), v.null()),
        softwareVersion: v.string(),
        medicalParametersVersion: v.union(v.string(), v.null()),
        configHash: v.union(v.string(), v.null()),
      }),
      payload: v.string(),
      signature: v.union(v.string(), v.null()),
    }),
  ),
  handler: async (ctx, args) => {
    const row = await ctx.db.get(args.recordId);
    if (row === null || (await readableMachine(ctx, row.machineId)) === null) return null;
    const person = async (userId: Id<"users">) => {
      const account = await ctx.db.get(userId);
      return account === null || account.clerkDeletedAt !== undefined
        ? { userId }
        : { userId, firstName: account.firstName, lastName: account.lastName };
    };
    // The server's own canonical text (`commissioningPayload`), never a client's.
    const { reference, report } = JSON.parse(row.payload) as RecordText;
    return {
      recordId: row._id,
      machineId: row.machineId,
      recordedAt: row.recordedAt,
      verdict: row.verdict,
      findings: row.findings,
      operator: await person(row.operator),
      attestedBy: await person(row.attestedBy),
      reference,
      report: {
        ranAt: report.ranAt,
        statement: report.attestation.statement,
        attestedAt: report.attestation.at,
        stoSource: report.stoSource,
        controls: report.controls,
        drive: report.drive,
        bitalinoAddress: report.bitalinoAddress,
        softwareVersion: report.softwareVersion,
        medicalParametersVersion: report.medicalParametersVersion,
        configHash: report.configHash,
      },
      payload: row.payload,
      signature: row.signature,
    };
  },
});
