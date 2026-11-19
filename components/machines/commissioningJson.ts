import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";

/**
 * The export of a commissioning record (ANH-115): the text the dashboard
 * wrote and signed, as it is stored, and its Ed25519 signature (`null` when
 * no key was set). Nothing is rewritten: checked offline against the
 * catalogue's public key, the bytes of `payload` are the signed ones.
 */
export type CommissioningRecord = NonNullable<
  FunctionReturnType<typeof api.machineCommissioning.getCommissioning>
>;

export function commissioningJson(
  record: Pick<CommissioningRecord, "payload" | "signature">,
): string {
  return `${JSON.stringify({ payload: record.payload, signature: record.signature }, null, 2)}\n`;
}

/** Its file name: the machine and the record's UTC day. */
export function commissioningFileName(
  record: Pick<CommissioningRecord, "machineId" | "recordedAt">,
): string {
  const day = new Date(record.recordedAt).toISOString().slice(0, 10);
  return `mise-en-service-${record.machineId}-${day}.json`;
}
