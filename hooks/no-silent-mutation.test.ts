import { readdirSync, readFileSync } from "node:fs";
import { dirname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

/**
 * Source check for the dashboard: a mutation is only called through
 * `useMutationWithFeedback`, and no handler next to one swallows an error.
 */

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));
const SCANNED_DIRECTORIES = ["app", "components"];

const DIRECT_MUTATION = /\buseMutation\b/;
const ANY_MUTATION = /\buseMutation(WithFeedback)?\b/;

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
  if (DIRECT_MUTATION.test(withoutComments(source))) {
    problems.push(
      "useMutation is used directly: call useMutationWithFeedback instead",
    );
  }
  if (!ANY_MUTATION.test(source)) return problems;
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

describe("ANH-156 EX-2 every mutation of app/ and components/", () => {
  const files = SCANNED_DIRECTORIES.flatMap((directory) =>
    sourceFiles(join(ROOT, directory)),
  ).map((path) => ({
    path: relative(ROOT, path).split("\\").join("/"),
    source: readFileSync(path, "utf8"),
  }));

  it("goes through useMutationWithFeedback, with no silent error handler", () => {
    const offenders = files.flatMap(({ path, source }) =>
      silentMutationProblems(source).map((problem) => `${path}: ${problem}`),
    );
    expect(offenders).toEqual([]);
  });

  it("is really looked at: the scan reads the files that call the hook", () => {
    const calling = files
      .filter(({ source }) => /\buseMutationWithFeedback\(/.test(source))
      .map(({ path }) => path);
    // Among them, the pages that came with their own messages.
    for (const expected of [
      "app/[locale]/dashboard/gestionnaires/[id]/page.tsx",
      "components/modals/MachineFormModal.tsx",
      "components/training/TrainingPanel.tsx",
    ]) {
      expect(calling, expected).toContain(expected);
    }
  });

  it("leaves the tests out: they replace useMutation by name", () => {
    expect(files.filter(({ path }) => /\.test\.tsx?$/.test(path))).toEqual([]);
  });
});
