// convex/_generated/api.d.ts, derived from the files of convex/ (ANH-195).
//
//     node scripts/ci/convex-generated-api.mjs            check: exit 1 when the committed file diverges
//     node scripts/ci/convex-generated-api.mjs --write    rewrite the committed file
//
// The file is committed because the site and the Convex functions do not
// type-check without it. It lists the modules of convex/, one import each, and
// nothing in it depends on a deployment as long as the backend mounts no
// component: its last line is then `export declare const components: {};`.
//
// The Convex command that writes it (`npx convex dev`, `npx convex codegen`)
// needs a deployment to talk to: since version 1.28 of the package, generating
// convex/_generated/ goes through the deployment (CHANGELOG.md of the package).
// The CI has no deployment and must reach none. This script is therefore what
// the `convex-tests` job runs: it applies the rule of the Convex command to the
// tree, renders the file in the form that command leaves, and compares.
//
// Both halves are copied from the installed package, and say from where:
//   - which file is a module: `entryPoints`, src/bundler/index.ts;
//   - the text: `codegenDynamicApiObjects` and `componentApiDTS`,
//     src/cli/codegen_templates/component_api.ts, as Prettier leaves it with
//     its default options (`writeFormattedFile`, src/cli/lib/codegen.ts).
// A later version of the package that changes either makes `npx convex dev`
// rewrite the file differently: the check then fails on the developer's diff,
// and this script must follow in the same pull request.
//
// A backend that mounts a component (a `convex.config.ts` under convex/) is
// refused: its `components` block comes from the deployment, not from the tree.

import { existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

/** The committed file, from the root of the repository. */
export const GENERATED = "convex/_generated/api.d.ts";

/** The extensions of a file that may define functions (`ENTRY_POINT_EXTENSIONS`). */
const EXTENSIONS = [".js", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".jsx"];

/** Names the generated file declares itself: a module of that name gets a `_`. */
const DECLARED = ["fullApi", "api", "internal", "components"];

/** Reserved words: legal as a property name, not as the name of an import. */
const RESERVED =
  "break case catch class const continue debugger default delete do else export extends false finally for function if import in instanceof new null return super switch this throw true try typeof var void while with let static yield await enum implements interface package private protected public".split(
    " ",
  );

/** Code unit order, the order of a Unix directory listing (`consistentPathSort`, `compareModulePaths`). */
const byCodeUnit = (/** @type {string} */ a, /** @type {string} */ b) => (a < b ? -1 : a > b ? 1 : 0);

/**
 * The modules of a Convex functions directory, as paths relative to it with
 * `/`, in the order the generated file lists them.
 * @param {string} directory the functions directory (convex/)
 * @returns {string[]}
 */
export function modulesOf(directory) {
  /** @type {string[]} */
  const found = [];
  /** @param {string} relative "" for the directory itself, else a path ending with "/" */
  const walk = (relative) => {
    const entries = readdirSync(join(directory, relative), { withFileTypes: true });
    for (const entry of entries.sort((a, b) => byCodeUnit(a.name, b.name))) {
      const path = `${relative}${entry.name}`;
      if (entry.isDirectory()) {
        if (existsSync(join(directory, path, "convex.config.ts"))) {
          throw new Error(
            `${path}/convex.config.ts defines a component: its bindings come from a deployment, this script cannot derive them`,
          );
        }
        walk(`${path}/`);
        continue;
      }
      // Neither a link nor anything else that is not a plain file.
      if (!entry.isFile()) continue;
      if (path.startsWith("_deps/")) throw new Error(`${path}: the _deps directory is reserved by Convex`);
      const name = entry.name;
      if (!EXTENSIONS.some((extension) => path.endsWith(extension))) continue;
      if (path.startsWith("_generated/")) continue;
      if (name.startsWith(".") || name.startsWith("#")) continue;
      if (name === "schema.ts" || name === "schema.js") continue;
      // `auth.config.ts`, `*.test.ts`, `*.fixtures.ts`: more than one dot is never a module.
      if (name.split(".").length > 2) continue;
      if (path.includes(" ")) continue;
      // A TypeScript file with neither import nor export is not a module.
      if (
        (name.endsWith(".ts") || name.endsWith(".tsx")) &&
        !/^\s{0,100}(import|export)/m.test(readFileSync(join(directory, path), "utf8"))
      ) {
        continue;
      }
      found.push(path);
    }
  };
  if (existsSync(join(directory, "convex.config.ts"))) {
    throw new Error("convex.config.ts mounts components: their bindings come from a deployment");
  }
  walk("");
  return found.sort(byCodeUnit);
}

/** The path a module is imported by: without its extension. @param {string} module */
const importPath = (module) => module.slice(0, module.lastIndexOf("."));

/** The name a module is imported under (`moduleIdentifier`). @param {string} module */
export function identifierOf(module) {
  const identifier = importPath(module).replaceAll("/", "_").replaceAll("-", "_");
  return DECLARED.includes(identifier) || RESERVED.includes(identifier) ? `${identifier}_` : identifier;
}

/** A property name as Prettier leaves it: quoted only when it has to be. @param {string} path */
const keyOf = (path) => (/^[A-Za-z_$][A-Za-z0-9_$]*$/.test(path) ? path : `"${path}"`);

/**
 * The text of api.d.ts for these modules.
 * @param {readonly string[]} modules as `modulesOf` returns them
 * @returns {string}
 */
export function render(modules) {
  const imports = modules.map((module) => `import type * as ${identifierOf(module)} from "../${importPath(module)}.js";`);
  const members = modules.map((module) => `  ${keyOf(importPath(module))}: typeof ${identifierOf(module)};`);
  return [
    "/* eslint-disable */",
    "/**",
    " * Generated `api` utility.",
    " *",
    " * THIS CODE IS AUTOMATICALLY GENERATED.",
    " *",
    " * To regenerate, run `npx convex dev`.",
    " * @module",
    " */",
    "",
    ...imports,
    "",
    "import type {",
    "  ApiFromModules,",
    "  FilterApi,",
    "  FunctionReference,",
    '} from "convex/server";',
    "",
    "declare const fullApi: ApiFromModules<{",
    ...members,
    "}>;",
    "",
    "/**",
    " * A utility for referencing Convex functions in your app's public API.",
    " *",
    " * Usage:",
    " * ```js",
    " * const myFunctionReference = api.myModule.myFunction;",
    " * ```",
    " */",
    "export declare const api: FilterApi<",
    "  typeof fullApi,",
    '  FunctionReference<any, "public">',
    ">;",
    "",
    "/**",
    " * A utility for referencing Convex functions in your app's internal API.",
    " *",
    " * Usage:",
    " * ```js",
    " * const myFunctionReference = internal.myModule.myFunction;",
    " * ```",
    " */",
    "export declare const internal: FilterApi<",
    "  typeof fullApi,",
    '  FunctionReference<any, "internal">',
    ">;",
    "",
    "export declare const components: {};",
    "",
  ].join("\n");
}

/** The modules a generated file imports, in its order. @param {string} text @returns {string[]} */
export function importedBy(text) {
  return [...text.matchAll(/^import type \* as \S+ from "\.\.\/(.+)\.js";$/gm)].map(([, path = ""]) => path);
}

/**
 * Compare the committed file with what the tree implies.
 * @param {string} root the root of the repository
 * @returns {{ ok: boolean, expected: string, lines: string[] }} `lines`: what to tell the reader
 */
export function check(root) {
  const modules = modulesOf(join(root, "convex"));
  const expected = render(modules);
  const file = join(root, GENERATED);
  const committed = existsSync(file) ? readFileSync(file, "utf8") : "";
  if (committed === expected) {
    return { ok: true, expected, lines: [`${GENERATED} lists the ${modules.length} modules of convex/.`] };
  }
  const wanted = modules.map(importPath);
  const listed = importedBy(committed);
  const lines = [`${GENERATED} is not what the files of convex/ imply.`];
  for (const path of wanted.filter((module) => !listed.includes(module))) lines.push(`  missing module: ${path}`);
  for (const path of listed.filter((module) => !wanted.includes(module))) lines.push(`  module without a file: ${path}`);
  if (lines.length === 1) lines.push("  same modules, another text: the order, a name or the fixed part differs");
  lines.push("Rewrite it with: node scripts/ci/convex-generated-api.mjs --write");
  return { ok: false, expected, lines };
}

/** @param {readonly string[]} argv @param {string} root @returns {number} exit code */
export function main(argv, root) {
  if (argv.length > 1 || (argv.length === 1 && argv[0] !== "--write")) {
    console.error("Usage: node scripts/ci/convex-generated-api.mjs [--write]");
    return 2;
  }
  let result;
  try {
    result = check(root);
  } catch (error) {
    console.error(`convex-generated-api: ${error instanceof Error ? error.message : String(error)}`);
    return 1;
  }
  if (argv[0] === "--write") {
    if (!result.ok) {
      mkdirSync(dirname(join(root, GENERATED)), { recursive: true });
      writeFileSync(join(root, GENERATED), result.expected);
    }
    console.log(result.ok ? result.lines[0] : `${GENERATED} rewritten.`);
    return 0;
  }
  for (const line of result.lines) (result.ok ? console.log : console.error)(line);
  return result.ok ? 0 : 1;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2), fileURLToPath(new URL("../..", import.meta.url)));
}
