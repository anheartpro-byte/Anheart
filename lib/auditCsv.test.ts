import { describe, expect, it } from "vitest";
import { auditCsv, AUDIT_CSV_COLUMNS, type AuditRow } from "./auditCsv";
import type { Id } from "../convex/_generated/dataModel";
import {
  AUDIT_ACTIONS,
  AUDIT_ACTOR_ROLES,
  AUDIT_SOURCES,
  AUDIT_TARGET_TYPES,
} from "../convex/lib/audit";
import fr from "../messages/fr.json";
import en from "../messages/en.json";

const row: AuditRow = {
  _id: "synthetic-audit" as Id<"audit_log">,
  at: Date.UTC(2026, 9, 16, 12, 30),
  organizationName: null,
  organizationId: undefined,
  actorId: undefined,
  details: undefined,
  actorRole: "system",
  actorLabel: null,
  action: "medical_parameters.registered",
  targetType: "medical_parameters_version",
  targetId: "synthetic-version",
  targetLabel: null,
  requestId: "synthetic-request",
  source: "operator",
};

describe("ANH-167 T03-e audit CSV", () => {
  it("uses stable English columns, UTC ISO dates, UTF-8 BOM, CRLF and empty optional cells", () => {
    expect(auditCsv([row])).toBe(
      "\uFEFFat,organizationId,organizationName,actorId,actorRole,actorLabel,action,targetType,targetId,targetLabel,details,requestId,source\r\n2026-10-16T12:30:00.000Z,,,,system,,medical_parameters.registered,medical_parameters_version,synthetic-version,,,synthetic-request,operator\r\n",
    );
    expect(AUDIT_CSV_COLUMNS).toHaveLength(13);
    expect(auditCsv([])).toBe(`\uFEFF${AUDIT_CSV_COLUMNS.join(",")}\r\n`);
  });

  it("escapes commas, quotes, carriage returns and line feeds according to RFC 4180 and serializes details as JSON", () => {
    const csv = auditCsv([
      {
        ...row,
        organizationName: 'Synthetic, "Centre"',
        actorLabel: "Synthetic\rActor\n",
        details: { fields: ["role"], count: 2 },
      },
    ]);
    expect(csv).toContain(',"Synthetic, ""Centre""",');
    expect(csv).toContain(',"Synthetic\rActor\n",');
    expect(csv).toContain(',"{""fields"":[""role""],""count"":2}",');
  });

  it.each(["=SUM(1)", "+1", "-1", "@SUM(1)", "\tformula", "\rformula"])(
    "neutralizes formula prefix %j before CSV escaping",
    (actorLabel) => {
      const csv = auditCsv([{ ...row, actorLabel }]);
      const expected = actorLabel.includes("\r")
        ? `"'${actorLabel}"`
        : `'${actorLabel}`;
      expect(csv).toContain(
        `,system,${expected},medical_parameters.registered,`,
      );
    },
  );

  it("leaves ordinary values unchanged and protects every text column", () => {
    expect(
      auditCsv([
        {
          ...row,
          organizationName: "=org",
          actorLabel: "+actor",
          targetLabel: "-target",
          requestId: "@request",
          targetId: "\tidentifier",
        },
      ]),
    ).toContain(
      ",'=org,,system,'+actor,medical_parameters.registered,medical_parameters_version,'\tidentifier,'-target,,'@request,operator",
    );
    expect(auditCsv([{ ...row, actorLabel: "Synthetic = 1" }])).toContain(
      ",system,Synthetic = 1,",
    );
  });
});

function atPath(value: unknown, path: string): unknown {
  for (const part of path.split(".")) {
    if (typeof value !== "object" || value === null || !(part in value))
      return undefined;
    value = (value as Record<string, unknown>)[part];
  }
  return value;
}

describe("ANH-167 T03-f closed audit labels", () => {
  it.each([
    ["fr", fr],
    ["en", en],
  ] as const)(
    "labels every real action, target, actor role and source in %s",
    (_locale, catalog) => {
      for (const [group, values] of [
        ["actions", AUDIT_ACTIONS],
        ["targets", AUDIT_TARGET_TYPES],
        ["roles", AUDIT_ACTOR_ROLES],
        ["sources", AUDIT_SOURCES],
      ] as const) {
        for (const value of values) {
          expect(
            atPath(catalog.audit, `${group}.${value}`),
            `${group}.${value}`,
          ).toBeTypeOf("string");
          expect(
            String(atPath(catalog.audit, `${group}.${value}`)).trim().length,
          ).toBeGreaterThan(0);
        }
      }
    },
  );
});
