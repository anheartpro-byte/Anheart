/// <reference types="vite/client" />
import { convexTest } from "convex-test";
import ts from "typescript";
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
const deployedModules = import.meta.glob(
  [
    "./**/*.ts",
    "!./_generated/**",
    "!./convex.config.ts",
    "!./**/*.test.ts",
    "!./**/*.fixtures.ts",
    "!./**/*.setup.ts",
    "!./**/*.matrix.ts",
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

function unsafeDatabaseIdCasts(text: string): string[] {
  const source = ts.createSourceFile(
    "source.ts",
    text,
    ts.ScriptTarget.Latest,
    true,
  );
  const unsafe: string[] = [];
  const aliases = new Set<string>();
  const findAliases = (node: ts.Node) => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      ts.isPropertyAccessExpression(node.initializer) &&
      node.initializer.name.text === "db"
    )
      aliases.add(node.name.text);
    ts.forEachChild(node, findAliases);
  };
  findAliases(source);
  const visit = (node: ts.Node) => {
    if (
      ts.isCallExpression(node) &&
      ts.isPropertyAccessExpression(node.expression) &&
      ["delete", "patch", "replace"].includes(node.expression.name.text)
    ) {
      const receiver = node.expression.expression;
      if (ts.isIdentifier(receiver) && aliases.has(receiver.text)) {
        unsafe.push(node.getText(source));
      } else if (
        ts.isPropertyAccessExpression(receiver) &&
        receiver.name.text === "db"
      ) {
        const id =
          ts.isStringLiteral(node.arguments[0]) && node.arguments.length > 1
            ? node.arguments[1]
            : node.arguments[0];
        const inspect = (value: ts.Node) => {
          if (ts.isAsExpression(value) || ts.isTypeAssertionExpression(value))
            unsafe.push(value.getText(source));
          ts.forEachChild(value, inspect);
        };
        if (id) inspect(id);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return unsafe;
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

  it.each([
    { added: 1000, removed: 0 },
    { added: 1001, removed: 0 },
    { added: 0, removed: 1001 },
    { added: 9000, removed: 9001 },
  ])(
    "C1 bounds identifier lists and preserves the total for $added added and $removed removed",
    async (lengths) => {
      const w = await world();
      const added = Array.from(
        { length: lengths.added },
        (_, i) => `synthetic-added-${i}`,
      );
      const removed = Array.from(
        { length: lengths.removed },
        (_, i) => `synthetic-removed-${i}`,
      );
      await w.t.run((ctx) =>
        audit(ctx, {
          source: "site",
          actor: { kind: "user", id: w.userId, role: "admin" },
          organizationId: w.organizationId,
          action: "gestionnaire_patients.set",
          targetId: w.userId,
          details: { added, removed },
        }),
      );
      const [row] = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
      expect(row.details?.added).toEqual(added.slice(0, 1000));
      expect(row.details?.removed).toEqual(removed.slice(0, 1000));
      expect(row.details?.count).toBe(
        lengths.added > 1000 || lengths.removed > 1000
          ? lengths.added + lengths.removed
          : undefined,
      );
    },
  );

  it.each(["added", "removed"] as const)(
    "C1 bounds an isolated %s list and replaces its count",
    async (key) => {
      const w = await world();
      const ids = Array.from({ length: 1001 }, (_, i) => `synthetic-${i}`);
      await w.t.run((ctx) =>
        audit(ctx, {
          source: "cron",
          actor: SYSTEM_ACTOR,
          organizationId: w.organizationId,
          action: "gestionnaire_patients.set",
          targetId: w.userId,
          details: { [key]: ids, count: 7 },
        }),
      );
      const [row] = await w.t.run((ctx) => ctx.db.query("audit_log").collect());
      expect(row.details).toEqual({ [key]: ids.slice(0, 1000), count: 1001 });
    },
  );

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
      expect(Object.keys(deployedModules), path).toContain(path);
      const functions = registrations(deployedModules[path]);
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
      expect(unsafeDatabaseIdCasts(text), path).toEqual([]);
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

  it.each(["delete", "patch", "replace"])(
    "C1 rejects unsafe id assertions for db.%s without matching comments or field values",
    (method) => {
      for (const cast of [
        "as never",
        "as any",
        'as Id<"users">',
        '<Id<"users">>',
      ]) {
        const argument = cast.startsWith("<") ? `${cast}id` : `id ${cast}`;
        expect(
          unsafeDatabaseIdCasts(`ctx.db.${method}(${argument}, {});`),
        ).toHaveLength(1);
        expect(
          unsafeDatabaseIdCasts(
            `ctx.db.${method}("users", (${argument}), {});`,
          ),
        ).toHaveLength(1);
      }
      expect(
        unsafeDatabaseIdCasts(
          `// ctx.db.${method}(id as never)\nctx.db.${method}(id, { text: "as any" });`,
        ),
      ).toEqual([]);
    },
  );

  it.each(["delete", "patch", "replace"])(
    "C2 R1-17 rejects every asserted id for %s",
    (method) => {
      for (const argument of [
        'id as GenericId<"users">',
        '(<GenericId<"users">>id)',
        'id as CustomIdentifier',
        '(id as unknown) as CustomIdentifier',
      ])
        for (const prefix of ["", '"users", '])
          expect(
            unsafeDatabaseIdCasts(`ctx.db.${method}(${prefix}${argument}, {});`),
          ).not.toEqual([]);
      expect(
        unsafeDatabaseIdCasts(
          `// const db = ctx.db; db.${method}(id as CustomIdentifier);\nctx.db.${method}(id, { text: "as CustomIdentifier", db: "ctx.db" });`,
        ),
      ).toEqual([]);
    },
  );

  it.each(["delete", "patch", "replace"])(
    "C2 R1-17 rejects database alias writes for %s",
    (method) => {
      for (const name of ["db", "database", "writer"])
        expect(
          unsafeDatabaseIdCasts(
            `const ${name} = ctx.db; ${name}.${method}(id, {});`,
          ),
        ).not.toEqual([]);
      expect(
        unsafeDatabaseIdCasts(
          `// const db = ctx.db; db.${method}(id as CustomIdentifier);\nctx.db.${method}(id, { text: "as CustomIdentifier", db: "ctx.db" });`,
        ),
      ).toEqual([]);
    },
  );

  it.each(["mirrorMembership", "getOrCreateUser"])(
    "C2 R1-20 reuses the loaded organisation without another get in %s",
    (name) => {
      const parsed = ts.createSourceFile(
        "users.ts",
        sources["./users.ts"],
        ts.ScriptTarget.Latest,
        true,
      );
      const reads: string[] = [];
      let found = false;
      const inspect = (node: ts.Node) => {
        if (
          ts.isCallExpression(node) &&
          node.expression.getText(parsed) === "ctx.db.get"
        )
          reads.push(node.getText(parsed));
        ts.forEachChild(node, inspect);
      };
      const visit = (node: ts.Node) => {
        if (
          (ts.isFunctionDeclaration(node) || ts.isVariableDeclaration(node)) &&
          node.name?.getText(parsed) === name
        ) {
          found = true;
          inspect(node);
        } else ts.forEachChild(node, visit);
      };
      visit(parsed);
      expect(found).toBe(true);
      expect(reads).toEqual([]);
    },
  );

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
  it("C1 loads every deployment source at every directory depth", () => {
    expect(Object.keys(deployedModules).sort()).toEqual(
      Object.keys(sources)
        .filter((path) => path !== "./convex.config.ts")
        .sort(),
    );
  });
  it("classifies every public and internal mutation exactly once, without stale entries", () => {
    const discovered = Object.entries(deployedModules)
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
      by_actor_role_and_at: ["actorRole", "at"],
      by_organization_and_actor_role_and_at: [
        "organizationId",
        "actorRole",
        "at",
      ],
      by_action_and_at: ["action", "at"],
      by_target_and_at: ["targetType", "targetId", "at"],
      by_target_type_and_at: ["targetType", "at"],
      by_organization_and_target_type_and_at: [
        "organizationId",
        "targetType",
        "at",
      ],
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
