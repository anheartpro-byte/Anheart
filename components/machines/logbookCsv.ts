import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";
import { cell } from "@/lib/auditCsv";

/**
 * The export of a machine's logbook (ANH-149), for the technical file:
 * one row per entry, identifiers only (who wrote it is a user id, never a
 * name). Same cell rules as `lib/auditCsv.ts` and the incidents export.
 */
export type LogbookEntry = NonNullable<
  FunctionReturnType<typeof api.machineLogbook.list>
>["entries"][number];

/** Each column: its French header and how a row fills it. */
export const LOGBOOK_CSV_COLUMNS: readonly (readonly [
  string,
  (entry: LogbookEntry) => string | number | undefined,
])[] = [
  ["identifiant_entree", (entry) => entry._id],
  ["date_utc", (entry) => new Date(entry.at).toISOString()],
  ["type", (entry) => entry.kind],
  ["resume", (entry) => entry.summary],
  ["details", (entry) => entry.details],
  ["ecrit_par", (entry) => entry.by],
  ["identifiant_incident", (entry) => entry.refs.incidentId],
  ["revision_configuration", (entry) => entry.refs.configRevision],
  ["version_logicielle", (entry) => entry.refs.softwareVersion],
  ["identifiant_piece_jointe", (entry) => entry.refs.storageId],
];

/** The file: UTF-8 with its mark, lines ended by CRLF, dates in ISO UTC, oldest first. */
export function logbookCsv(entries: readonly LogbookEntry[]): string {
  const lines = [LOGBOOK_CSV_COLUMNS.map(([header]) => header).join(",")];
  for (const entry of [...entries].sort((a, b) => a.at - b.at)) {
    lines.push(
      LOGBOOK_CSV_COLUMNS.map(([, value]) => {
        const shown = value(entry);
        return cell(shown === undefined ? "" : String(shown));
      }).join(","),
    );
  }
  return `﻿${lines.join("\r\n")}\r\n`;
}
