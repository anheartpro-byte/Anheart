import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";

/**
 * The export of the riders' feedback (ANH-146), for medical analysis: one row
 * per session, crossed with its summary. Identifiers only: the server sends
 * no name, no e-mail address and no comment (`sessionFeedback.exportFeedback`),
 * and nothing is added here. Same cell rules as `lib/auditCsv.ts`.
 */
export type FeedbackExportRow = FunctionReturnType<
  typeof api.sessionFeedback.exportFeedback
>["page"][number];

/** Each column: its French header and the field of a row it holds. */
export const FEEDBACK_CSV_COLUMNS = [
  ["identifiant_seance", "sessionId"],
  ["identifiant_organisation", "organizationId"],
  ["identifiant_machine", "machineId"],
  ["identifiant_passager", "userId"],
  ["debut_utc", "startedAt"],
  ["duree_s", "durationS"],
  ["pic_g_pointe_pied", "legTipMaxG"],
  ["pic_variation_g_pointe_pied_g_par_s", "legTipMaxGRate"],
  ["nausee_0_10", "nauseaScore"],
  ["malaise_vertige_0_10", "dizzinessScore"],
  ["ressenti", "comfort"],
  ["saisi_par", "submittedBy"],
  ["saisi_le_utc", "submittedAt"],
] as const satisfies readonly (readonly [string, keyof FeedbackExportRow])[];

const DATES: ReadonlySet<keyof FeedbackExportRow> = new Set([
  "startedAt",
  "submittedAt",
]);

/** A cell a spreadsheet will not read as a formula, quoted when it must be. */
function cell(value: string) {
  const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value;
  return /[",\r\n]/.test(safe) ? `"${safe.replaceAll('"', '""')}"` : safe;
}

function valueOf(row: FeedbackExportRow, field: keyof FeedbackExportRow) {
  const value = row[field];
  if (value === null) return "";
  return DATES.has(field) ? new Date(value).toISOString() : String(value);
}

/** The file: UTF-8 with its mark, lines ended by CRLF, dates in ISO UTC. */
export function feedbackCsv(rows: readonly FeedbackExportRow[]): string {
  const lines = [FEEDBACK_CSV_COLUMNS.map(([header]) => header).join(",")];
  for (const row of rows) {
    lines.push(
      FEEDBACK_CSV_COLUMNS.map(([, field]) => cell(valueOf(row, field))).join(
        ",",
      ),
    );
  }
  return `﻿${lines.join("\r\n")}\r\n`;
}
