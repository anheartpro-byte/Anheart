import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import ts from "typescript";

/**
 * Source check for the dashboard: a mutation is only called through
 * `useMutationWithFeedback`, an action through `useActionWithFeedback`, and
 * no handler next to one swallows an error. Clients from `useConvex()`,
 * `new ConvexReactClient` or `new ConvexHttpClient` may read with `.query`.
 * Syntax inspection refuses access to their `mutation` and `action` methods,
 * including extraction, bind/call/apply, optional chains, literal bracket keys
 * and destructuring. Extracting a write method is refused before it is called.
 * Remaining limits include variable-computed keys (`convex[name]`), aliased
 * imports, clients passed between modules or stored in containers, and lexical
 * shadowing of client variable names. This is not an exhaustive list or a type
 * analysis. Error-handler inspection still reads text within the same file.
 */

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SCANNED_DIRECTORIES = ["app", "components", "lib", "hooks"];

/** The two hooks that wrap the Convex calls: the only files allowed to make them. */
const WRAPPERS = [
  "hooks/use-mutation-with-feedback.ts",
  "hooks/use-action-with-feedback.ts",
];

const DIRECT_CALLS: ReadonlyArray<{ pattern: RegExp; problem: string }> = [
  {
    pattern: /\buseMutation\b/,
    problem:
      "useMutation is used directly: call useMutationWithFeedback instead",
  },
  {
    pattern: /\buseAction\b/,
    problem: "useAction is used directly: call useActionWithFeedback instead",
  },
];
/** A Convex client held by the page, and what writes through it. */
const CONVEX_CLIENT = /\b(useConvex|ConvexReactClient|ConvexHttpClient)\b/;
const ANY_CALL =
  /\b(useMutation|useMutationWithFeedback|useAction|useActionWithFeedback|useConvex)\b/;

function clientWrite(source: string): boolean {
  const tree = ts.createSourceFile(
    "source.tsx",
    source,
    ts.ScriptTarget.Latest,
    false,
    ts.ScriptKind.TSX,
  );
  const clients = new Set<string>();
  const isClient = (expression: ts.Expression): boolean =>
    (ts.isIdentifier(expression) && clients.has(expression.text)) ||
    (ts.isCallExpression(expression) &&
      ts.isIdentifier(expression.expression) &&
      expression.expression.text === "useConvex") ||
    (ts.isNewExpression(expression) &&
      ts.isIdentifier(expression.expression) &&
      (expression.expression.text === "ConvexReactClient" ||
        expression.expression.text === "ConvexHttpClient"));
  const collect = (node: ts.Node): void => {
    if (
      ts.isVariableDeclaration(node) &&
      ts.isIdentifier(node.name) &&
      node.initializer &&
      isClient(node.initializer)
    ) {
      clients.add(node.name.text);
    }
    ts.forEachChild(node, collect);
  };
  collect(tree);

  const isWrite = (name: string): boolean =>
    name === "mutation" || name === "action";
  const visit = (node: ts.Node): boolean | undefined => {
    if (
      ts.isPropertyAccessExpression(node) &&
      isClient(node.expression) &&
      isWrite(node.name.text)
    ) {
      return true;
    }
    if (
      ts.isElementAccessExpression(node) &&
      isClient(node.expression) &&
      ts.isStringLiteralLike(node.argumentExpression) &&
      isWrite(node.argumentExpression.text)
    ) {
      return true;
    }
    if (
      ts.isVariableDeclaration(node) &&
      ts.isObjectBindingPattern(node.name) &&
      node.initializer &&
      isClient(node.initializer) &&
      node.name.elements.some((element) => {
        const name = element.propertyName ?? element.name;
        return (
          (ts.isIdentifier(name) || ts.isStringLiteralLike(name)) &&
          isWrite(name.text)
        );
      })
    ) {
      return true;
    }
    return ts.forEachChild(node, visit);
  };
  return visit(tree) === true;
}

function withoutComments(code: string): string {
  return code.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/[^\n]*/g, "");
}

/** Where the bracket at `open` is closed, or -1 when it never is. */
function closingIndex(source: string, open: number): number {
  const opening = source[open];
  const closing = opening === "{" ? "}" : ")";
  let depth = 0;
  for (let i = open; i < source.length; i++) {
    if (source[i] === opening) depth++;
    else if (source[i] === closing && --depth === 0) return i;
  }
  return -1;
}

/** The text between the bracket at `open` and the bracket that closes it. */
function enclosed(source: string, open: number): string {
  const close = closingIndex(source, open);
  return source.slice(open + 1, close === -1 ? undefined : close);
}

/**
 * Whether a handler body shows nothing: it is empty, or it holds only calls to
 * the console. Read one call at a time, each character once, so that a long or
 * odd body cannot stall the check.
 */
function showsNothing(body: string): boolean {
  const consoleCall = /console\.\w+\s*\(/y;
  let at = 0;
  const skipSpaces = () => {
    while (at < body.length && body[at].trim() === "") at++;
  };
  skipSpaces();
  while (at < body.length) {
    consoleCall.lastIndex = at;
    const call = consoleCall.exec(body);
    if (!call) return false;
    const close = closingIndex(body, at + call[0].length - 1);
    if (close === -1) return false;
    at = close + 1;
    skipSpaces();
    if (body[at] === ";") at++;
    skipSpaces();
  }
  return true;
}

/** The body of every `catch` block and of every `.catch(...)` handler. */
function errorHandlers(source: string): string[] {
  const handlers: string[] = [];
  for (const block of source.matchAll(/\bcatch\s*(?:\([^)]*\)\s*)?\{/g)) {
    handlers.push(enclosed(source, block.index + block[0].length - 1));
  }
  for (const call of source.matchAll(/\.catch\s*\(/g)) {
    const handler = enclosed(source, call.index + call[0].length - 1).trim();
    const arrowAt = handler.indexOf("=>");
    if (arrowAt === -1) {
      // A function passed by name: only the console is known to show nothing.
      handlers.push(/^console\.\w+$/.test(handler) ? `${handler}()` : handler);
      continue;
    }
    const returned = handler.slice(arrowAt + 2).trimStart();
    if (returned.startsWith("{")) {
      handlers.push(enclosed(returned, 0));
    } else {
      handlers.push(/^(undefined|null|void 0)$/.test(returned) ? "" : returned);
    }
  }
  return handlers.map(withoutComments);
}

/** What is wrong in one source file; empty when nothing is. */
export function silentMutationProblems(source: string): string[] {
  const problems: string[] = [];
  const code = withoutComments(source);
  for (const { pattern, problem } of DIRECT_CALLS) {
    if (pattern.test(code)) problems.push(problem);
  }
  if (CONVEX_CLIENT.test(code) && clientWrite(source)) {
    problems.push(
      "a Convex client writes directly: call useMutationWithFeedback or useActionWithFeedback instead",
    );
  }
  if (!ANY_CALL.test(code)) return problems;
  for (const handler of errorHandlers(source)) {
    if (showsNothing(handler)) {
      problems.push(
        handler.trim() === ""
          ? "an error handler is empty"
          : "an error handler only writes to the console",
      );
    }
  }
  return problems;
}

/** The site's sources, not its tests: a test replaces `useMutation` by name. */
function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sourceFiles(path);
    return /\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)
      ? [path]
      : [];
  });
}

describe("ANH-156 EX-2 detection of a silent mutation", () => {
  const direct = `
    const deleteMachine = useMutation(api.machines.deleteMachine);
    const handleDelete = async () => {
      try {
        await deleteMachine({ machineId });
      } catch (err) {
        BODY
      }
    };`;

  it("refuses useMutation( followed by an empty catch", () => {
    expect(silentMutationProblems(direct.replace("BODY", ""))).toEqual([
      "useMutation is used directly: call useMutationWithFeedback instead",
      "an error handler is empty",
    ]);
  });

  it("refuses useMutation( followed by a lone console.error", () => {
    expect(
      silentMutationProblems(direct.replace("BODY", "console.error(err);")),
    ).toEqual([
      "useMutation is used directly: call useMutationWithFeedback instead",
      "an error handler only writes to the console",
    ]);
  });

  it("refuses useMutation( even when its catch shows the error", () => {
    expect(
      silentMutationProblems(direct.replace("BODY", "setError(String(err));")),
    ).toEqual([
      "useMutation is used directly: call useMutationWithFeedback instead",
    ]);
  });

  const throughHook = `
    const save = useMutationWithFeedback(api.users.updatePatient);
    const copy = async () => {
      HANDLER
    };`;

  it.each([
    ["an empty catch", "try { await copyKey(); } catch {}", "empty"],
    [
      "a catch holding only a comment",
      "try { await copyKey(); } catch (e) { /* ignore */ }",
      "empty",
    ],
    [
      "a catch that only logs, on several lines",
      'try { await copyKey(); } catch (e) {\n console.error("copy", e);\n console.warn(format(e));\n }',
      "console",
    ],
    ["an empty .catch handler", "copyKey().catch(() => {});", "empty"],
    ["a .catch that returns nothing", "copyKey().catch(() => null);", "empty"],
    [
      "a .catch given the console",
      "copyKey().catch(console.error);",
      "console",
    ],
    [
      "a .catch arrow that only logs",
      "copyKey().catch((e) => console.error(e));",
      "console",
    ],
  ])("refuses %s beside the hook", (_label, handler, kind) => {
    expect(
      silentMutationProblems(throughHook.replace("HANDLER", handler)),
    ).toEqual([
      kind === "empty"
        ? "an error handler is empty"
        : "an error handler only writes to the console",
    ]);
  });

  it.each([
    ["no handler at all", "await save({ userId });"],
    [
      "a catch that shows the error",
      "try { await copyKey(); } catch (e) { console.error(e); setError(t('common.error')); }",
    ],
    ["a .catch that shows the error", "copyKey().catch(showCopyFailure);"],
  ])("accepts %s beside the hook", (_label, handler) => {
    expect(
      silentMutationProblems(throughHook.replace("HANDLER", handler)),
    ).toEqual([]);
  });

  it("answers at once on a handler holding thousands of console calls", () => {
    // The check reads each character once: its time grows with the body.
    const calls = "console.a()" + " console.a()".repeat(5000);
    const handler = (body: string) =>
      throughHook.replace(
        "HANDLER",
        `try { await copyKey(); } catch (e) { ${body} }`,
      );

    const started = performance.now();
    const onlyLogs = silentMutationProblems(handler(calls));
    const thenShows = silentMutationProblems(handler(`${calls} setError(e);`));
    const elapsedMs = performance.now() - started;

    expect(onlyLogs).toEqual(["an error handler only writes to the console"]);
    expect(thenShows).toEqual([]);
    expect(elapsedMs).toBeLessThan(500);
  });

  it("reads console calls whatever the depth of their brackets", () => {
    expect(
      silentMutationProblems(
        throughHook.replace(
          "HANDLER",
          "try { await copyKey(); } catch (e) { console.error(a(b(c(d(e))))); }",
        ),
      ),
    ).toEqual(["an error handler only writes to the console"]);
  });

  it("leaves a file without any mutation alone", () => {
    expect(
      silentMutationProblems("try { draw(); } catch (e) { console.error(e); }"),
    ).toEqual([]);
  });
});

describe("ANH-194 EX-3 the other ways to write through Convex", () => {
  it("refuses useAction( used directly, followed by a silent catch", () => {
    expect(
      silentMutationProblems(`
        const create = useAction(api.users.createPatient);
        try { await create(args); } catch (e) { console.error(e); }`),
    ).toEqual([
      "useAction is used directly: call useActionWithFeedback instead",
      "an error handler only writes to the console",
    ]);
  });

  it.each([
    [
      "the client kept in a constant",
      "const convex = useConvex();\n await convex.mutation(api.machines.deleteMachine, { machineId });",
    ],
    [
      "the client used where it is taken",
      "await useConvex().mutation(api.machines.deleteMachine, { machineId });",
    ],
    [
      "an action of the client",
      "const convex = useConvex();\n await convex.action(api.users.createPatient, args);",
    ],
    [
      "a client made by hand",
      "const client = new ConvexReactClient(url);\n client.mutation(api.x.y, {});",
    ],
  ])("refuses a mutation sent by a Convex client: %s", (_label, source) => {
    expect(silentMutationProblems(source)).toEqual([
      "a Convex client writes directly: call useMutationWithFeedback or useActionWithFeedback instead",
    ]);
  });

  describe.each([
    ["useConvex", "useConvex()"],
    ["ConvexReactClient", "new ConvexReactClient(url)"],
    ["ConvexHttpClient", "new ConvexHttpClient(url)"],
  ])("ANH-232 EX-5 writes through %s", (_client, initializer) => {
    it.each([
      [
        "bind followed by a call",
        "const send = convex.mutation.bind(convex); await send(api.x.y, {});",
      ],
      ["call", "await convex.mutation.call(convex, api.x.y, {});"],
      ["apply", "await convex.action.apply(convex, [api.x.y, {}]);"],
      [
        "a method alias followed by a call",
        "const send = convex.mutation; await send(api.x.y, {});",
      ],
      ["an optional call", "await convex.mutation?.(api.x.y, {});"],
      ["an optional client", "await convex?.mutation(api.x.y, {});"],
      ["double-quoted brackets", 'await convex["mutation"](api.x.y, {});'],
      ["single-quoted brackets", "await convex['action'](api.x.y, {});"],
      ["a literal template key", "await convex[`mutation`](api.x.y, {});"],
      [
        "a destructured action alias",
        "const { action: run } = convex; await run(api.x.y, {});",
      ],
      ["spaces after the dot", "await convex.  mutation(api.x.y, {});"],
      ["a newline after the dot", "await convex.\n action(api.x.y, {});"],
    ])("refuses %s", (_label, write) => {
      expect(
        silentMutationProblems(`const convex = ${initializer};\n${write}`),
      ).toEqual([
        "a Convex client writes directly: call useMutationWithFeedback or useActionWithFeedback instead",
      ]);
    });

    it("accepts queries and an unrelated object's action property", () => {
      expect(
        silentMutationProblems(`
          const convex = ${initializer};
          const filter = { action: state.action };
          await convex.query(api.audit.list, filter);`),
      ).toEqual([]);
    });
  });

  it("refuses a mutation destructured directly from useConvex", () => {
    expect(
      silentMutationProblems(
        "const { mutation } = useConvex(); await mutation(api.x.y, {});",
      ),
    ).toEqual([
      "a Convex client writes directly: call useMutationWithFeedback or useActionWithFeedback instead",
    ]);
  });

  it("accepts a client that only reads, and checks the handlers beside it", () => {
    const reads =
      "const convex = useConvex();\n const page = await convex.query(api.recordStorage.list, args);";
    expect(silentMutationProblems(reads)).toEqual([]);
    expect(
      silentMutationProblems(`${reads}\n download().catch(() => {});`),
    ).toEqual(["an error handler is empty"]);
  });

  it("checks the handlers beside useActionWithFeedback", () => {
    expect(
      silentMutationProblems(`
        const create = useActionWithFeedback(api.users.createPatient);
        copyKey().catch(console.error);`),
    ).toEqual(["an error handler only writes to the console"]);
  });

  it("does not read a name in a comment as a call", () => {
    expect(
      silentMutationProblems(
        "// useMutation and useAction go through the hooks\nconst x = 1;",
      ),
    ).toEqual([]);
  });
});

describe("ANH-156 EX-2 every mutation of app/, components/, lib/ and hooks/", () => {
  const files = SCANNED_DIRECTORIES.flatMap((directory) =>
    sourceFiles(join(ROOT, directory)),
  ).map((path) => ({
    path: relative(ROOT, path).split("\\").join("/"),
    source: readFileSync(path, "utf8"),
  }));

  it("goes through useMutationWithFeedback or useActionWithFeedback, with no silent error handler", () => {
    const offenders = files
      .filter(({ path }) => !WRAPPERS.includes(path))
      .flatMap(({ path, source }) =>
        silentMutationProblems(source).map((problem) => `${path}: ${problem}`),
      );
    expect(offenders).toEqual([]);
  });

  it("lets only the two wrapping hooks call Convex directly, and they do", () => {
    for (const wrapper of WRAPPERS) {
      const file = files.find(({ path }) => path === wrapper);
      expect(file, wrapper).toBeDefined();
      expect(silentMutationProblems(file?.source ?? "")).toHaveLength(1);
    }
  });

  it("is really looked at: the scan reads the files that call the hooks or hold a client", () => {
    const calling = (pattern: RegExp) =>
      files
        .filter(({ source }) => pattern.test(source))
        .map(({ path }) => path);
    // Among them, the pages that came with their own messages.
    for (const expected of [
      "app/[locale]/dashboard/gestionnaires/[id]/page.tsx",
      "components/modals/MachineFormModal.tsx",
      "components/training/TrainingPanel.tsx",
    ]) {
      expect(calling(/\buseMutationWithFeedback\(/), expected).toContain(
        expected,
      );
    }
    expect(calling(/\buseActionWithFeedback\(/)).toContain(
      "components/modals/PatientFormModal.tsx",
    );
    expect(calling(/\buseConvex\(/)).toContain(
      "components/training/SessionRecordCard.tsx",
    );
  });

  it("leaves the tests out: they replace useMutation by name", () => {
    expect(files.filter(({ path }) => /\.test\.tsx?$/.test(path))).toEqual([]);
  });
});
