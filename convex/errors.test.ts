/// <reference types="vite/client" />
/**
 * ANH-194 EX-5: the dashboard's refusals carry a stable code from
 * `convex/lib/errors.ts`, and the site has a French and an English sentence
 * for each of them (`errors.<code>` in `messages/*.json`).
 */
import { ConvexError } from "convex/values";
import { describe, expect, it } from "vitest";
import ts from "typescript";
import en from "../messages/en.json";
import fr from "../messages/fr.json";
import { DASHBOARD_ERROR_CODES, DASHBOARD_ERRORS, refusal } from "./lib/errors";

// Every deployable source of the backend, as text.
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

const catalogs = { fr: fr.errors, en: en.errors } as Record<
  string,
  Record<string, string>
>;

describe("ANH-194 EX-5 the table of error codes", () => {
  it("has a French and an English sentence for every code", () => {
    for (const [language, catalog] of Object.entries(catalogs)) {
      const missing = DASHBOARD_ERROR_CODES.filter(
        (code) => (catalog[code] ?? "").trim() === "",
      );
      expect(missing, language).toEqual([]);
    }
  });

  it("leaves no translation without its code", () => {
    for (const [language, catalog] of Object.entries(catalogs)) {
      expect(Object.keys(catalog).sort(), language).toEqual(
        [...DASHBOARD_ERROR_CODES].sort(),
      );
    }
  });

  it("holds codes a translation key can name: one lowercase token each", () => {
    for (const code of DASHBOARD_ERROR_CODES) {
      expect(code).toMatch(/^[a-z][a-z0-9_]*$/);
      expect(DASHBOARD_ERRORS[code].length).toBeGreaterThan(0);
    }
  });

  it("refuses with a ConvexError whose data is the code alone", () => {
    const error = refusal("machine_in_session");

    expect(error).toBeInstanceOf(ConvexError);
    expect(error.data).toBe("machine_in_session");
    expect(error.message).toBe("machine_in_session");
  });

  it("is where every code thrown by the backend comes from", () => {
    // A code written in place, `new ConvexError("some_code")` or
    // `new ConvexError({ code: "some_code", ... })`, must be in the table
    // too: otherwise the site would show the bare code.
    const thrown = new Set<string>();
    for (const text of Object.values(sources)) {
      for (const match of text.matchAll(
        /new ConvexError(?:<[^>]*>)?\(\s*(?:\{\s*code:\s*)?"([a-z][a-z0-9_]*)"/g,
      )) {
        thrown.add(match[1]);
      }
    }
    // The account limiter's code is one of them (ANH-166).
    expect(thrown).toContain("rate_limited");
    expect(
      [...thrown].filter((code) => !(code in DASHBOARD_ERRORS)),
    ).toEqual([]);
  });

  it("is used for every refusal of the machines and accounts functions", () => {
    // Convex masks a plain Error in production: these two modules no longer
    // throw one, except the heartbeat, which answers the machine, not a page.
    expect(plainErrors("./users.ts")).toEqual([]);
    expect(plainErrors("./machines.ts")).toEqual(["Machine not found"]);
    expect(sources["./machines.ts"]).toContain(
      "// Read by the heartbeat route, which answers the machine",
    );
  });
});

/** The texts of the plain `Error`s a source throws (`throw new Error("...")`, `throw Error("...")`). */
function plainErrors(path: string): string[] {
  return [...sources[path].matchAll(/throw (?:new )?Error\(\s*"([^"]*)"/g)].map(
    (match) => match[1],
  );
}

/** The texts of the `ConvexError`s a source throws with a sentence, not a code. */
function sentences(path: string): string[] {
  return [
    ...sources[path].matchAll(
      /new ConvexError(?:<[^>]*>)?\(\s*(["`])([^"`]*)\1/g,
    ),
  ]
    .map((match) => match[2])
    .filter((text) => !/^[a-z][a-z0-9_]*$/.test(text));
}

/** Find direct error construction, inheritance, literal throws and ConvexError imports. */
export function trainingErrorViolations(text: string): string[] {
  const source = ts.createSourceFile(
    "training.ts",
    text,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const namespaces = new Set<string>();
  const violations: string[] = [];
  const globalErrors = new Set([
    "Error",
    "TypeError",
    "RangeError",
    "SyntaxError",
    "ReferenceError",
    "EvalError",
    "URIError",
    "AggregateError",
  ]);
  const isGlobalError = (expression: ts.Expression) =>
    (ts.isIdentifier(expression) && globalErrors.has(expression.text)) ||
    (ts.isPropertyAccessExpression(expression) &&
      ts.isIdentifier(expression.expression) &&
      expression.expression.text === "globalThis" &&
      globalErrors.has(expression.name.text));
  for (const statement of source.statements) {
    if (!ts.isImportDeclaration(statement)) continue;
    const bindings = statement.importClause?.namedBindings;
    if (
      bindings &&
      ts.isNamespaceImport(bindings) &&
      ts.isStringLiteral(statement.moduleSpecifier) &&
      statement.moduleSpecifier.text === "convex/values"
    ) {
      namespaces.add(bindings.name.text);
    } else if (bindings && ts.isNamedImports(bindings)) {
      for (const binding of bindings.elements) {
        if ((binding.propertyName ?? binding.name).text === "ConvexError") {
          violations.push(binding.getText(source));
        }
      }
    }
  }
  const visit = (node: ts.Node) => {
    if (
      (ts.isNewExpression(node) || ts.isCallExpression(node)) &&
      (isGlobalError(node.expression) ||
        (ts.isIdentifier(node.expression) &&
          node.expression.text === "ConvexError") ||
        (ts.isPropertyAccessExpression(node.expression) &&
          node.expression.name.text === "ConvexError"))
    ) {
      violations.push(node.getText(source));
    }
    if (
      ts.isHeritageClause(node) &&
      node.token === ts.SyntaxKind.ExtendsKeyword &&
      node.types.some((type) => isGlobalError(type.expression))
    ) {
      violations.push(node.getText(source));
    }
    if (
      ts.isThrowStatement(node) &&
      (ts.isStringLiteral(node.expression) ||
        ts.isNoSubstitutionTemplateLiteral(node.expression) ||
        ts.isTemplateExpression(node.expression))
    ) {
      violations.push(node.getText(source));
    }
    if (
      ts.isPropertyAccessExpression(node) &&
      ts.isIdentifier(node.expression) &&
      namespaces.has(node.expression.text) &&
      node.name.text === "ConvexError"
    ) {
      violations.push(node.getText(source));
    }
    ts.forEachChild(node, visit);
  };
  visit(source);
  return violations;
}

describe("T04-a: training's error guard", () => {
  it.each(
    [
      "Error",
      "TypeError",
      "RangeError",
      "SyntaxError",
      "ReferenceError",
      "EvalError",
      "URIError",
      "AggregateError",
    ].flatMap((name) =>
      [name, `globalThis.${name}`].flatMap((constructor) => [
        `new ${constructor}(message)`,
        `${constructor}(message)`,
        `class Refused extends ${constructor} {}`,
      ]),
    ),
  )("R1c1-3: detects a global error constructor: %s", (text) => {
    expect(trainingErrorViolations(text)).not.toEqual([]);
  });

  it.each([
    'throw "Session not found"',
    "throw `Session not found`",
    "throw `Session ${sessionId} not found`",
    'import { ConvexError as CE } from "./lib/errors";',
    'import { type ConvexError as CE } from "another-module";',
  ])("R1c1-3: detects a literal throw or named import: %s", (text) => {
    expect(trainingErrorViolations(text)).not.toEqual([]);
  });

  it.each([
    'import { ConvexError } from "convex/values";',
    'import { ConvexError as CE } from "convex/values"; throw new CE("x")',
    'import * as values from "convex/values"; throw new values.ConvexError(code)',
    'import { ConvexError } from "./lib/errors"; throw new ConvexError("phrase")',
    'throw new ConvexError("phrase")',
    "throw new errors.ConvexError(code)",
    "throw ConvexError(code)",
    "throw errors.ConvexError(code)",
    "throw new Error(message)",
    "throw Error(message)",
    'throw new Error("x")',
    'throw Error("x")',
  ])("detects a bypass: %s", (text) => {
    expect(trainingErrorViolations(text)).not.toEqual([]);
  });

  it.each([
    'throw machineError("session_not_found", "Session not found")',
    "throw refusal(code)",
    "// new Error(x)",
    '// throw new ConvexError("phrase"); errors.ConvexError(code)',
    '"new ConvexError(phrase); errors.ConvexError(code)"',
    '/* import { ConvexError as CE } from "convex/values"; Error(x) */',
    '"Error("',
    "`new Error(x); values.ConvexError`",
    "const text = 'import { ConvexError } from \"convex/values\"';",
    'import * as values from "convex/values"; values.v.string()',
    '// new TypeError(message); globalThis.Error(message); throw "phrase"',
    '/* class Refused extends globalThis.RangeError {} */',
    '"throw `phrase`; new globalThis.SyntaxError(message)"',
    '"class Refused extends Error {}"',
    'try { throw refusal(code); } catch (err) { throw err; }',
  ])("allows coded refusals and inert text: %s", (text) => {
    expect(trainingErrorViolations(text)).toEqual([]);
  });

  it("keeps the real training.ts on coded refusals", () => {
    expect(trainingErrorViolations(sources["./training.ts"])).toEqual([]);
  });

  it.each([
    "const E = Error; new E(message)",
    "Reflect.construct(Error, [message])",
    'const { ConvexError: CE } = await import("convex/values"); new CE(message)',
    'new globalThis["Error"](message)',
    'import * as values from "convex/values"; new values["ConvexError"](message)',
  ])("R1c1-3: leaves indirect forms outside the guard's scope: %s", (text) => {
    expect(trainingErrorViolations(text)).toEqual([]);
  });
});

describe("ANH-90 no English sentence reaches the site", () => {
  it("signs in with codes: a refusal of the sign-in is read in production too", () => {
    // `requireAuth` and `getCurrentUserOrThrow` once threw plain Errors,
    // which Convex masks in production.
    expect(plainErrors("./lib/auth.ts")).toEqual([]);
    expect(sentences("./lib/auth.ts")).toEqual([]);
    for (const code of [
      "not_authenticated",
      "account_not_registered",
      "no_active_organization",
      "organization_not_synchronized",
      "unknown_organization_role",
      "malformed_organization_claim",
      "no_organization",
    ]) {
      expect(code in DASHBOARD_ERRORS).toBe(true);
    }
  });

  it("refuses with codes in the training, releases and records functions", () => {
    expect(sentences("./softwareReleases.ts")).toEqual([]);
    expect(plainErrors("./softwareReleases.ts")).toEqual([]);
    // The only plain Errors of the records module are its machine's routes'.
    expect(plainErrors("./recordStorage.ts")).toEqual([
      "Manifest pages are incomplete",
      "Invalid manifest page",
    ]);
    expect(sentences("./notifications.ts")).toEqual([]);
    expect(plainErrors("./notifications.ts")).toEqual([]);
    // training.ts refuses with codes only (ANH-90, after ANH-144): it builds
    // no ConvexError of its own. The launcher's refusals are codes too: the
    // type of `launcherRefusal` is `DashboardErrorCode | null`.
    expect(trainingErrorViolations(sources["./training.ts"])).toEqual([]);
  });
});
