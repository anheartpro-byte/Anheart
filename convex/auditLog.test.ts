/// <reference types="vite/client" />
import { convexTest } from "convex-test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Doc } from "./_generated/dataModel";
import type { CurrentUser } from "./lib/auth";
import * as auditModule from "./lib/audit";
import {
  AUDIT_ACTIONS,
  AUDIT_ACTION_TARGET,
  AUDIT_ACTOR_ROLES,
  AUDIT_DETAIL_KEYS,
  AUDIT_SOURCES,
  AUDIT_TARGET_TYPES,
  NON_SENSITIVE_MUTATIONS,
  SENSITIVE_MUTATIONS,
  SYSTEM_ACTOR,
  audit,
  auditDetailsValidator,
  machineActor,
  userActor,
} from "./lib/audit";
import schema from "./schema";

const modules = import.meta.glob([
  "./**/*.ts",
  "!./**/*.test.ts",
  "!./**/*.fixtures.ts",
  "!./**/*.setup.ts",
  "!./**/*.matrix.ts",
]);
const sources = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
  ],
  { eager: true, query: "?raw", import: "default" },
) as Record<string, string>;
const topLevel = import.meta.glob(
  [
    "./*.ts",
    "./migrations/*.ts",
    "!./convex.config.ts",
    "!./*.test.ts",
    "!./*.fixtures.ts",
    "!./*.setup.ts",
    "!./*.matrix.ts",
  ],
  { eager: true },
) as Record<string, Record<string, unknown>>;

type Registration = {
  isQuery?: boolean;
  isMutation?: boolean;
  isAction?: boolean;
  isHttp?: boolean;
};

function registrations(mod: Record<string, unknown>): [string, Registration][] {
  return Object.entries(mod).filter(
    (entry): entry is [string, Registration] => {
      const value = entry[1] as Registration | null;
      return (
        value !== null &&
        (typeof value === "function" || typeof value === "object") &&
        (value.isQuery === true ||
          value.isMutation === true ||
          value.isAction === true ||
          value.isHttp === true)
      );
    },
  );
}

const NOW = 1_800_000_000_000;
const baseRow = {
  at: NOW,
  actorRole: "system",
  action: "medical_parameters.registered",
  targetType: "medical_parameters_version",
  targetId: "synthetic-version-id",
  requestId: "synthetic-request-id",
  source: "operator",
} satisfies Omit<Doc<"audit_log">, "_id" | "_creationTime">;

async function world() {
  const t = convexTest(schema, modules);
  const ids = await t.run(async (ctx) => {
    const organizationId = await ctx.db.insert("organizations", {
      name: "Synthetic audit organisation",
      slug: "synthetic-audit",
      createdAt: NOW,
      settings: { requirePrescription: false, language: "fr" },
    });
    const userId = await ctx.db.insert("users", {
      organizationId,
      clerkId: "synthetic-audit-user",
      role: "admin",
      firstName: "Synthetic",
      lastName: "Audit",
      email: "audit@example.test",
      language: "fr",
      createdAt: NOW,
    });
    const machineId = await ctx.db.insert("machines", {
      organizationId,
      name: "Synthetic audit machine",
      status: "offline",
      lastHeartbeat: NOW,
      createdAt: NOW,
    });
    const user = await ctx.db.get("users", userId);
    if (!user) throw new Error("Synthetic audit user missing");
    return { organizationId, userId, machineId, user };
  });
  return { t, ...ids };
}

describe("ANH-167 T01-a audit insertion", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
  });
  afterEach(() => vi.useRealTimers());

  it.each(["admin", "org_admin", "gestionnaire", "user"] as const)(
    "writes exactly one complete row for a user acting as %s",
    async (role) => {
      const w = await world();
      const caller: CurrentUser = {
        ...w.user,
        role,
        organizationId: w.organizationId,
      };
      const details = { role, previousRole: "user" as const, fields: ["role"] };
      await w.t.run(async (ctx) => {
        const metadata = vi.spyOn(ctx.meta, "getRequestMetadata");
        const insert = vi.spyOn(ctx.db, "insert");
        await audit(ctx, {
          source: "site",
          actor: userActor(caller),
          organizationId: w.organizationId,
          action: "user.role_changed",
          targetId: w.userId,
          details,
        });
        expect(metadata).toHaveBeenCalledTimes(1);
        expect(insert).toHaveBeenCalledTimes(1);
      });
      const rows = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
      expect(rows).toEqual([
        {
          ...rows[0],
          at: NOW,
          organizationId: w.organizationId,
          actorId: w.userId,
          actorRole: role,
          action: "user.role_changed",
          targetType: "user",
          targetId: w.userId,
          details,
          source: "site",
          requestId: expect.any(String),
        },
      ]);
      expect(rows[0].requestId.length).toBeGreaterThan(0);
    },
  );

  it("writes exactly one complete row for a machine", async () => {
    const w = await world();
    await w.t.run((ctx) =>
      audit(ctx, {
        source: "machine",
        actor: machineActor(w.machineId),
        organizationId: w.organizationId,
        action: "session.started",
        targetId: "synthetic-session-id",
        details: { machineId: w.machineId },
      }),
    );
    const rows = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(rows).toEqual([
      {
        ...rows[0],
        at: NOW,
        organizationId: w.organizationId,
        actorId: w.machineId,
        actorRole: "machine",
        action: "session.started",
        targetType: "session",
        targetId: "synthetic-session-id",
        details: { machineId: w.machineId },
        source: "machine",
        requestId: expect.any(String),
      },
    ]);
    expect(rows[0].requestId.length).toBeGreaterThan(0);
  });

  it.each(["cron", "webhook", "operator"] as const)(
    "writes exactly one system row without an actor ID for %s",
    async (source) => {
      const w = await world();
      await w.t.run((ctx) =>
        audit(ctx, {
          source,
          actor: SYSTEM_ACTOR,
          organizationId: undefined,
          action: "medical_parameters.registered",
          targetId: "synthetic-version-id",
        }),
      );
      const rows = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
      expect(rows).toEqual([
        {
          _id: expect.any(String),
          _creationTime: expect.any(Number),
          at: NOW,
          actorRole: "system",
          action: "medical_parameters.registered",
          targetType: "medical_parameters_version",
          targetId: "synthetic-version-id",
          source,
          requestId: expect.any(String),
        },
      ]);
      expect(rows[0].requestId.length).toBeGreaterThan(0);
    },
  );

  it("shares the originating request ID between two calls in one mutation", async () => {
    const t = convexTest(schema, modules);
    const requestId = await t.run(async (ctx) => {
      const metadata = await ctx.meta.getRequestMetadata();
      for (const action of [
        "session.cancelled",
        "session.stop_requested",
      ] as const) {
        await audit(ctx, {
          source: "site",
          actor: SYSTEM_ACTOR,
          organizationId: undefined,
          action,
          targetId: "synthetic-session-id",
        });
      }
      return metadata.requestId;
    });
    const rows = await t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(rows).toHaveLength(2);
    expect(rows.map((row) => row.requestId)).toEqual([requestId, requestId]);
    expect(requestId.length).toBeGreaterThan(0);
  });

  it("derives a valid target type for every closed action", async () => {
    const t = convexTest(schema, modules);
    await t.run(async (ctx) => {
      for (const action of AUDIT_ACTIONS) {
        await audit(ctx, {
          source: "operator",
          actor: SYSTEM_ACTOR,
          organizationId: undefined,
          action,
          targetId: `synthetic-${action}`,
        });
      }
    });
    const rows = await t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(rows).toHaveLength(AUDIT_ACTIONS.length);
    for (const row of rows) {
      expect(row.targetType).toBe(AUDIT_ACTION_TARGET[row.action]);
      expect(AUDIT_TARGET_TYPES).toContain(row.targetType);
    }
  });
});

describe("ANH-167 T01-b append-only and T01-c no purge", () => {
  it("enumerates table access and permits only the schema, insert helper and query modules", () => {
    const touching = Object.keys(sources)
      .filter((path) => sources[path].includes("audit_log"))
      .sort();
    const queryModules = touching.filter(
      (path) => path !== "./schema.ts" && path !== "./lib/audit.ts",
    );
    expect(touching).toEqual(
      ["./lib/audit.ts", "./schema.ts", ...queryModules].sort(),
    );
    for (const path of queryModules) {
      expect(path, path).toMatch(/^\.\/[^/]+\.ts$/);
      expect(Object.keys(topLevel), path).toContain(path);
      const functions = registrations(topLevel[path]);
      expect(functions.length, path).toBeGreaterThan(0);
      for (const [name, registration] of functions) {
        expect(registration.isQuery, `${path}:${name}`).toBe(true);
        expect(registration.isMutation, `${path}:${name}`).not.toBe(true);
        expect(registration.isAction, `${path}:${name}`).not.toBe(true);
        expect(registration.isHttp, `${path}:${name}`).not.toBe(true);
      }
    }
    for (const path of touching) {
      expect(sources[path], path).not.toMatch(
        /\.\s*(?:patch|replace|delete)\s*\(/,
      );
    }
    for (const [path, text] of Object.entries(sources)) {
      expect(text, path).not.toMatch(
        /\b(?:patch|replace|delete|normalizeId|id)\s*\(\s*["'`]audit_log["'`]/,
      );
    }
    const helper = sources["./lib/audit.ts"];
    expect([...helper.matchAll(/["'`]audit_log["'`]/g)]).toHaveLength(1);
    expect(helper).toMatch(/ctx\.db\.insert\("audit_log",/);
    expect(
      [...helper.matchAll(/ctx\.db\.(\w+)\s*\(/g)].map((match) => match[1]),
    ).toEqual(["insert"]);
    expect(helper).not.toMatch(/\bthrow\s+(?:new|refusal)\b/);
  });

  it("exports only the fixed audit API, with no reader or updater", () => {
    const names = [
      "AUDIT_SOURCES",
      "auditSourceValidator",
      "AUDIT_ACTOR_ROLES",
      "auditActorRoleValidator",
      "AUDIT_TARGET_TYPES",
      "auditTargetTypeValidator",
      "AUDIT_ACTIONS",
      "auditActionValidator",
      "AUDIT_ACTION_TARGET",
      "auditDetailsValidator",
      "AUDIT_DETAIL_KEYS",
      "SENSITIVE_MUTATIONS",
      "NON_SENSITIVE_MUTATIONS",
      "userActor",
      "machineActor",
      "SYSTEM_ACTOR",
      "audit",
    ];
    expect(Object.keys(auditModule).sort()).toEqual(names.sort());
    const exported = [
      ...sources["./lib/audit.ts"].matchAll(
        /\bexport\s+(?:async\s+)?(?:const|function|type)\s+(\w+)/g,
      ),
    ].map((match) => match[1]);
    expect(exported.sort()).toEqual(
      [
        ...names,
        "AuditSource",
        "AuditActorRole",
        "AuditTargetType",
        "AuditAction",
        "AuditActor",
      ].sort(),
    );
    expect(sources["./lib/audit.ts"]).not.toMatch(
      /\bexport\s+(?:type\s+)?(?:\{|\*)/,
    );
  });

  it("never schedules audit purging or references the audit module in crons", () => {
    expect(sources["./crons.ts"]).not.toMatch(
      /audit_log|["']\.\/.*audit["']|internal\.audit\b/,
    );
  });
});

describe("ANH-167 T01-d mutation classification", () => {
  it("classifies every public and internal mutation exactly once, without stale entries", () => {
    const discovered = Object.entries(topLevel)
      .flatMap(([path, mod]) =>
        registrations(mod)
          .filter(([, value]) => value.isMutation === true)
          .map(([name]) => `${path.slice(2, -3)}.${name}`),
      )
      .sort();
    const sensitive = Object.keys(SENSITIVE_MUTATIONS);
    const nonSensitive = Object.keys(NON_SENSITIVE_MUTATIONS);
    expect(discovered.length).toBeGreaterThan(0);
    expect(sensitive.filter((name) => name in NON_SENSITIVE_MUTATIONS)).toEqual(
      [],
    );
    expect([...sensitive, ...nonSensitive].sort()).toEqual(discovered);
    for (const reason of Object.values(NON_SENSITIVE_MUTATIONS))
      expect(reason.trim().length).toBeGreaterThan(0);
  });

  it("produces every action and assigns only closed actions to sensitive mutations", () => {
    const actions = Object.values(SENSITIVE_MUTATIONS).flat();
    expect([...new Set(actions)].sort()).toEqual([...AUDIT_ACTIONS].sort());
    for (const mutationActions of Object.values(SENSITIVE_MUTATIONS))
      expect(mutationActions.length).toBeGreaterThan(0);
    expect(Object.keys(AUDIT_ACTION_TARGET).sort()).toEqual(
      [...AUDIT_ACTIONS].sort(),
    );
  });
});

describe("ANH-167 T01-e closed schema and details", () => {
  it("allows exactly the reviewed detail keys, all optional and without health or free-text keys", () => {
    const expected = [
      "fields",
      "role",
      "previousRole",
      "level",
      "previousLevel",
      "version",
      "softwareVersion",
      "medicalParametersVersion",
      "machineId",
      "userId",
      "gestionnaireId",
      "added",
      "removed",
      "count",
      "outcome",
      "expiresAt",
    ];
    expect([...AUDIT_DETAIL_KEYS]).toEqual(expected);
    expect(Object.keys(auditDetailsValidator.fields).sort()).toEqual(
      expected.sort(),
    );
    expect(
      Object.values(auditDetailsValidator.fields).every(
        (field) => field.isOptional === "optional",
      ),
    ).toBe(true);
    for (const key of [
      "hrMax",
      "birthYear",
      "notes",
      "reason",
      "bpm",
      "answers",
    ]) {
      expect(AUDIT_DETAIL_KEYS).not.toContain(key);
    }
  });

  it.each([
    "hrMax",
    "birthYear",
    "notes",
    "reason",
    "bpm",
    "answers",
    "unreviewed",
  ])(
    "rejects an insertion containing the unreviewed detail key %s",
    async (key) => {
      const t = convexTest(schema, modules);
      await expect(
        t.run((ctx) =>
          ctx.db.insert("audit_log", {
            ...baseRow,
            details: {
              fields: ["synthetic-field"],
              [key]: "synthetic-forbidden-value",
            },
          }),
        ),
      ).rejects.toThrow();
      expect(await t.run((ctx) => ctx.db.query("audit_log").collect())).toEqual(
        [],
      );
    },
  );

  it("accepts all reviewed details with synthetic identifiers and versions", async () => {
    const w = await world();
    const details = {
      fields: ["role"],
      role: "org_admin" as const,
      previousRole: "user" as const,
      level: "auto_validated" as const,
      previousLevel: "none" as const,
      version: "synthetic-1",
      softwareVersion: "pi-0.0.1",
      medicalParametersVersion: "synthetic-medical-1",
      machineId: w.machineId,
      userId: w.userId,
      gestionnaireId: w.userId,
      added: [w.userId],
      removed: [w.machineId],
      count: 1,
      outcome: "completed" as const,
      expiresAt: NOW,
    };
    await w.t.run((ctx) => ctx.db.insert("audit_log", { ...baseRow, details }));
    const rows = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
    expect(rows).toHaveLength(1);
    expect(rows[0].details).toEqual(details);
  });

  it.each(["action", "targetType", "actorRole", "source"])(
    "refuses an unknown %s",
    async (field) => {
      const t = convexTest(schema, modules);
      await expect(
        t.run((ctx) =>
          ctx.db.insert("audit_log", {
            ...baseRow,
            [field]: "synthetic-unknown-value",
          }),
        ),
      ).rejects.toThrow();
    },
  );

  it("keeps all closed source, role and target values in the schema", () => {
    const fields = schema.tables.audit_log.validator.fields;
    expect(fields.source.members.map((value) => value.value)).toEqual([
      ...AUDIT_SOURCES,
    ]);
    expect(fields.actorRole.members.map((value) => value.value)).toEqual([
      ...AUDIT_ACTOR_ROLES,
    ]);
    expect(fields.targetType.members.map((value) => value.value)).toEqual([
      ...AUDIT_TARGET_TYPES,
    ]);
    expect(fields.action.members.map((value) => value.value)).toEqual([
      ...AUDIT_ACTIONS,
    ]);
    expect(Object.keys(fields).sort()).toEqual(
      [
        "at",
        "organizationId",
        "actorId",
        "actorRole",
        "action",
        "targetType",
        "targetId",
        "details",
        "requestId",
        "source",
      ].sort(),
    );
  });

  it("declares every time-ordered global and organisation filter index", () => {
    expect(
      Object.fromEntries(
        schema.tables.audit_log[" indexes"]().map((index) => [
          index.indexDescriptor,
          index.fields,
        ]),
      ),
    ).toEqual({
      by_at: ["at"],
      by_organization_and_at: ["organizationId", "at"],
      by_actor_and_at: ["actorId", "at"],
      by_action_and_at: ["action", "at"],
      by_target_and_at: ["targetType", "targetId", "at"],
      by_organization_and_actor_and_at: ["organizationId", "actorId", "at"],
      by_organization_and_action_and_at: ["organizationId", "action", "at"],
      by_organization_and_target_and_at: [
        "organizationId",
        "targetType",
        "targetId",
        "at",
      ],
    });
  });
});
