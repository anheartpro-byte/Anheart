// ANH-195 EX-11: what scripts/ci/convex-generated-api.mjs takes for a module of
// convex/, the text it renders, and what its check answers. Every tree here is
// a throwaway directory; the committed file of this repository is judged by
// the `convex-tests` job, which runs the script itself.

import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import { GENERATED, check, identifierOf, importedBy, main, modulesOf, render } from "./convex-generated-api.mjs";

const MODULE = "export const value = 1;\n";

/**
 * A throwaway repository root holding these files.
 * @param {import("node:test").TestContext} context @param {Record<string, string>} files
 */
function tree(context, files) {
  const root = mkdtempSync(join(tmpdir(), "anheart-convex-api-"));
  context.after(() => rmSync(root, { recursive: true, force: true }));
  for (const [path, content] of Object.entries(files)) {
    mkdirSync(dirname(join(root, path)), { recursive: true });
    writeFileSync(join(root, path), content);
  }
  return root;
}

/** Run `main` with its output captured. @param {string[]} argv @param {string} root */
function run(argv, root) {
  /** @type {string[]} */
  const printed = [];
  const { log, error } = console;
  console.log = console.error = (/** @type {unknown} */ line) => void printed.push(String(line));
  try {
    return { status: main(argv, root), output: printed.join("\n") };
  } finally {
    console.log = log;
    console.error = error;
  }
}

test("EX-11 a module is a file the Convex command would deploy, and nothing else", (context) => {
  const root = tree(context, {
    "convex/machines.ts": MODULE,
    "convex/lib/auth.ts": "import { v } from 'convex/values';\n",
    "convex/migrations/deep/one.ts": MODULE,
    "convex/plain.js": "module.exports = {};\n",
    // Not modules, each for its own reason.
    "convex/schema.ts": MODULE,
    "convex/auth.config.ts": MODULE,
    "convex/machines.test.ts": MODULE,
    "convex/machineAuth.fixtures.ts": MODULE,
    "convex/test.setup.ts": MODULE,
    "convex/_generated/api.d.ts": MODULE,
    "convex/_generated/server.js": MODULE,
    "convex/.hidden.ts": MODULE,
    "convex/#draft.ts": MODULE,
    "convex/with space.ts": MODULE,
    "convex/notes only.ts": "// nothing here yet\n",
    "convex/empty.ts": "// nothing here yet\n",
    "convex/README.md": "# Convex\n",
    "convex/VERSION": "cloud-0.0.0-dev\n",
    "convex/tsconfig.json": "{}\n",
  });
  symlinkSync(join(root, "convex/machines.ts"), join(root, "convex/linked.ts"));

  const modules = modulesOf(join(root, "convex"));

  assert.deepEqual(modules, ["lib/auth.ts", "machines.ts", "migrations/deep/one.ts", "plain.js"]);
});

test("EX-11 the modules are listed in code unit order of their path, extension included", (context) => {
  const names = ["sessions.ts", "sessionSummaries.ts", "machines.ts", "lib/zeta.ts", "Zeta.ts", "alpha.ts", "lib.ts"];
  const root = tree(context, Object.fromEntries(names.map((name) => [`convex/${name}`, MODULE])));

  const modules = modulesOf(join(root, "convex"));

  assert.deepEqual(modules, [
    "Zeta.ts",
    "alpha.ts",
    "lib.ts",
    "lib/zeta.ts",
    "machines.ts",
    "sessionSummaries.ts",
    "sessions.ts",
  ]);
});

test("EX-11 a module is imported under a name that is a legal identifier", () => {
  const named = ["lib/auth.ts", "my-module.ts", "api.ts", "internal.ts", "delete.ts", "new.ts", "users.ts"].map(
    (module) => [module, identifierOf(module)],
  );

  assert.deepEqual(named, [
    ["lib/auth.ts", "lib_auth"],
    ["my-module.ts", "my_module"],
    ["api.ts", "api_"],
    ["internal.ts", "internal_"],
    ["delete.ts", "delete_"],
    ["new.ts", "new_"],
    ["users.ts", "users"],
  ]);
});

test("EX-11 the text names each module twice, quoted only where its path needs it", () => {
  const text = render(["crons.ts", "lib/auth.ts", "my-module.ts"]);

  assert.deepEqual(importedBy(text), ["crons", "lib/auth", "my-module"]);
  assert.ok(
    text.includes(
      [
        "declare const fullApi: ApiFromModules<{",
        "  crons: typeof crons;",
        '  "lib/auth": typeof lib_auth;',
        '  "my-module": typeof my_module;',
        "}>;",
      ].join("\n"),
    ),
    text,
  );
  assert.ok(text.startsWith("/* eslint-disable */\n/**\n * Generated `api` utility.\n"));
  assert.ok(text.endsWith("\nexport declare const components: {};\n"));
});

test("EX-11 the check passes on the file it wrote, and says what a stale file lacks or keeps", (context) => {
  const root = tree(context, {
    "convex/machines.ts": MODULE,
    "convex/lib/auth.ts": MODULE,
    [GENERATED]: "",
  });

  // Nothing committed yet: every module is missing.
  const empty = run([], root);
  assert.equal(empty.status, 1);
  assert.match(empty.output, /is not what the files of convex\/ imply\.\n {2}missing module: lib\/auth\n {2}missing module: machines\n/);
  assert.match(empty.output, /Rewrite it with: node scripts\/ci\/convex-generated-api\.mjs --write$/);

  const written = run(["--write"], root);
  assert.equal(written.status, 0);
  assert.equal(readFileSync(join(root, GENERATED), "utf8"), render(["lib/auth.ts", "machines.ts"]));
  const fresh = run([], root);
  assert.equal(fresh.status, 0);
  assert.match(fresh.output, /lists the 2 modules of convex\/\.$/);

  // A module is added, another is removed: the committed file names neither change.
  writeFileSync(join(root, "convex/lib/contract.ts"), MODULE);
  rmSync(join(root, "convex/machines.ts"));
  const stale = run([], root);
  assert.equal(stale.status, 1);
  assert.match(stale.output, /\n {2}missing module: lib\/contract\n {2}module without a file: machines\n/);
  assert.equal(check(root).ok, false);

  // The check rewrites nothing; --write does, and says nothing is left to do the second time.
  assert.deepEqual(importedBy(readFileSync(join(root, GENERATED), "utf8")), ["lib/auth", "machines"]);
  assert.equal(run(["--write"], root).status, 0);
  assert.deepEqual(importedBy(readFileSync(join(root, GENERATED), "utf8")), ["lib/auth", "lib/contract"]);
  assert.match(run(["--write"], root).output, /lists the 2 modules/);
});

test("EX-11 the check refuses a file edited by hand though it names the right modules", (context) => {
  const root = tree(context, {
    "convex/machines.ts": MODULE,
    [GENERATED]: render(["machines.ts"]).replace("components: {}", "components: any"),
  });

  const edited = run([], root);

  assert.equal(edited.status, 1);
  assert.match(edited.output, /same modules, another text/);
});

test("EX-11 the check refuses a file that is missing, and --write creates it", (context) => {
  const root = tree(context, { "convex/machines.ts": MODULE });

  const missing = run([], root);
  const written = run(["--write"], root);

  assert.equal(missing.status, 1);
  assert.match(missing.output, /missing module: machines/);
  assert.equal(written.status, 0);
  assert.equal(readFileSync(join(root, GENERATED), "utf8"), render(["machines.ts"]));
});

test("EX-11 a backend that mounts a component is refused: its bindings are not in the tree", (context) => {
  const mounted = tree(context, { "convex/machines.ts": MODULE, "convex/convex.config.ts": MODULE });
  const nested = tree(context, { "convex/machines.ts": MODULE, "convex/billing/convex.config.ts": MODULE });
  const reserved = tree(context, { "convex/machines.ts": MODULE, "convex/_deps/left.ts": MODULE });

  const answers = [mounted, nested, reserved].map((root) => run([], root));

  assert.deepEqual(
    answers.map(({ status }) => status),
    [1, 1, 1],
  );
  assert.match(answers[0]?.output ?? "", /convex\.config\.ts mounts components/);
  assert.match(answers[1]?.output ?? "", /billing\/convex\.config\.ts defines a component/);
  assert.match(answers[2]?.output ?? "", /_deps directory is reserved/);
});

test("EX-11 an argument the script does not know is refused before anything is read", (context) => {
  const root = tree(context, { "convex/machines.ts": MODULE });

  const answers = [["--fix"], ["--write", "--write"]].map((argv) => run(argv, root));

  assert.deepEqual(
    answers.map(({ status }) => status),
    [2, 2],
  );
  assert.match(answers[0]?.output ?? "", /^Usage: /);
});
