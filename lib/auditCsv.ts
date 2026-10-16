import type { FunctionReturnType } from "convex/server";
import type { api } from "../convex/_generated/api";

export type AuditRow = FunctionReturnType<
  typeof api.audit.list
>["page"][number];

export const AUDIT_CSV_COLUMNS = [
  "at",
  "organizationId",
  "organizationName",
  "actorId",
  "actorRole",
  "actorLabel",
  "action",
  "targetType",
  "targetId",
  "targetLabel",
  "details",
  "requestId",
  "source",
] as const;

function cell(value: string) {
  const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe;
}

export function auditCsv(rows: readonly AuditRow[]): string {
  const lines = [AUDIT_CSV_COLUMNS.join(",")];
  for (const row of rows) {
    lines.push(
      AUDIT_CSV_COLUMNS.map((column) => {
        const value =
          column === "at"
            ? new Date(row.at).toISOString()
            : column === "details"
              ? row.details === undefined
                ? ""
                : JSON.stringify(row.details)
              : (row[column] ?? "");
        return cell(value);
      }).join(","),
    );
  }
  return `\uFEFF${lines.join("\r\n")}\r\n`;
}
