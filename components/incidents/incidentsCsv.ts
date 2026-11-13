import type { FunctionReturnType } from "convex/server";
import type { api } from "@/convex/_generated/api";
import { cell } from "@/lib/auditCsv";

/**
 * The export of the incidents (ANH-145), for the risk analysis (ANH-103) and
 * the technical file (ANH-105): one row per incident the Anheart admin reads,
 * identifiers, codes and the analysis. Same cell rules as `lib/auditCsv.ts`.
 */
export type IncidentRow = FunctionReturnType<typeof api.incidents.list>[number];

/** Each column: its French header and the field of a row it holds. */
export const INCIDENT_CSV_COLUMNS = [
  ["identifiant_incident", "_id"],
  ["ouvert_le_utc", "openedAt"],
  ["identifiant_organisation", "organizationId"],
  ["identifiant_machine", "machineId"],
  ["machine", "machineName"],
  ["identifiant_seance", "sessionId"],
  ["type", "kind"],
  ["regle", "rule"],
  ["instant_machine_utc", "t"],
  ["gravite", "severity"],
  ["personne_a_bord", "occupied"],
  ["statut", "status"],
  ["cause", "analysis"],
  ["action", "action"],
  ["analyse_par", "analyzedBy"],
  ["clos_par", "closedBy"],
  ["clos_le_utc", "closedAt"],
  ["identifiant_fichier_refuse", "recordId"],
] as const satisfies readonly (readonly [string, keyof IncidentRow])[];

const DATES: ReadonlySet<keyof IncidentRow> = new Set(["openedAt", "t", "closedAt"]);

function valueOf(row: IncidentRow, field: keyof IncidentRow): string {
  const value = row[field];
  if (value === undefined) return "";
  return DATES.has(field) ? new Date(value as number).toISOString() : String(value);
}

/** The file: UTF-8 with its mark, lines ended by CRLF, dates in ISO UTC. */
export function incidentsCsv(rows: readonly IncidentRow[]): string {
  const lines = [INCIDENT_CSV_COLUMNS.map(([header]) => header).join(",")];
  for (const row of rows) {
    lines.push(
      INCIDENT_CSV_COLUMNS.map(([, field]) => cell(valueOf(row, field))).join(","),
    );
  }
  return `﻿${lines.join("\r\n")}\r\n`;
}
