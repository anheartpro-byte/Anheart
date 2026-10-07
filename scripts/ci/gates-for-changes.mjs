// Which gates a pull request must run, decided from the files it changes.
//
//     git diff --name-only --no-renames -z BASE HEAD \
//         | node scripts/ci/gates-for-changes.mjs --event pull_request
//
// Prints two lines for $GITHUB_OUTPUT, `python=true|false` (pi-gate and
// simulation-gate) and `node=true|false` (web and convex-tests), and explains
// itself on standard error. `docs` and `audit` are not decided here: they
// always run.
//
// The rule can only ever take a gate away for a reason written below. A gate
// is skipped when EVERY changed file is of a kind listed as unable to affect
// it. A file of no listed kind, an empty list, an event that is not a pull
// request, a path this script cannot read: all of these run everything.
//
// Limits, stated rather than hidden. The lists say what the gates read TODAY:
// a new test that reads a file from the other side (a Python test opening a
// page of docs/, a site module importing a JSON file of raspberry-pi/) makes
// a list wrong without touching this file. That is why every push to develop
// and every nightly run ignore this script and run every gate. And the script
// that decides is the pull request's own copy: a change to it, like any
// change under .github/ or scripts/ci/, runs everything and must be reviewed.

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

/** The only event whose runs may skip a gate. */
export const FILTERED_EVENT = "pull_request";

/**
 * Checked first, whatever the lists below say: these run every gate.
 * @type {ReadonlyArray<{ name: string, test: (path: string) => boolean }>}
 */
export const EVERYTHING = [
  { name: "CI definition", test: (path) => path.startsWith(".github/") },
  { name: "CI helper", test: (path) => path.startsWith("scripts/ci/") },
  {
    name: "dependency manifest or lockfile",
    test: (path) =>
      ["package.json", "package-lock.json", "bun.lock", "requirements.txt", "pyproject.toml"].includes(path) ||
      /^(raspberry-pi|simulation|deploy)\/(.*\/)?(requirements[^/]*\.txt|pyproject\.toml)$/.test(path),
  },
  {
    // Read by the Pi gate (Python tests open these files) AND by the web job
    // (node --test raspberry-pi/tests/web, ESLint): neither side may skip.
    name: "console files tested on both sides",
    test: (path) => path.startsWith("raspberry-pi/src/web/static/") || path.startsWith("raspberry-pi/tests/web/"),
  },
  {
    // What the Pi and the cloud agree on, whatever its extension (Markdown
    // included): each side has tests of its own against it.
    name: "contract shared by the Pi and the cloud",
    test: (path) => path.startsWith("contracts/"),
  },
  {
    // Markdown, but not documentation: the release tooling reads it, and its
    // tests with it. Until a gate is known to be the only one that does, all run.
    name: "changelog read by the release tooling",
    test: (path) => /(^|\/)changelog\.md$/i.test(path),
  },
];

const DOCUMENT_EXTENSIONS = [".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf"];
const SITE_DIRECTORIES = ["app/", "components/", "hooks/", "i18n/", "lib/", "messages/", "public/"];
const SITE_FILES = [
  "next.config.ts",
  "proxy.ts",
  "tsconfig.json",
  "eslint.config.mjs",
  "postcss.config.mjs",
  "components.json",
];
/** The Vitest configurations of the site and of Convex, at the root: vitest.<suite>.config.mts. */
const SITE_TEST_CONFIGURATION = /^vitest\.[a-z0-9-]+\.config\.mts$/;
const PYTHON_DIRECTORIES = ["raspberry-pi/", "simulation/"];
const PYTHON_EXTENSIONS = [".py", ".pyi"];

/** @param {string} path @param {ReadonlyArray<string>} extensions */
const endsWithOneOf = (path, extensions) => extensions.some((extension) => path.endsWith(extension));
/** @param {string} path @param {ReadonlyArray<string>} directories */
const isUnder = (path, directories) => directories.some((directory) => path.startsWith(directory));

/**
 * The allow-lists. Each kind says which gates a file of that kind CANNOT
 * affect; the first kind that matches is the file's kind.
 * @type {ReadonlyArray<{ kind: string, python: boolean, node: boolean, test: (path: string) => boolean }>}
 */
export const KINDS = [
  {
    // Markdown anywhere (the changelog apart, see above), and the images of
    // docs/. No gate reads them: the docs job does, and it always runs.
    kind: "documentation",
    python: false,
    node: false,
    test: (path) => path.endsWith(".md") || (path.startsWith("docs/") && endsWithOneOf(path, DOCUMENT_EXTENSIONS)),
  },
  {
    // The scope of the SonarQube Cloud analysis, at the root and under that
    // name only. Its one reader is the scanner of .github/workflows/sonar.yml,
    // a workflow of its own that starts whatever this rule answers, and that
    // tests the file first. No gate opens it: not the Python tools, not
    // TypeScript, ESLint, the node tests or the Next.js build.
    kind: "static analysis scope",
    python: false,
    node: false,
    test: (path) => path === "sonar-project.properties",
  },
  {
    // The Next.js site. The Pi and the simulation read nothing from it.
    kind: "site",
    python: false,
    node: true,
    test: (path) =>
      isUnder(path, SITE_DIRECTORIES) || SITE_FILES.includes(path) || SITE_TEST_CONFIGURATION.test(path),
  },
  {
    // Convex functions and their tests. The Pi and the simulation read nothing from them.
    kind: "convex",
    python: false,
    node: true,
    test: (path) => path.startsWith("convex/"),
  },
  {
    // Python sources and stubs of the Pi and of the simulation, and nothing
    // else of those directories: TypeScript, ESLint, node --test and the
    // Next.js build read no Python file.
    kind: "python",
    python: true,
    node: false,
    test: (path) => isUnder(path, PYTHON_DIRECTORIES) && endsWithOneOf(path, PYTHON_EXTENSIONS),
  },
];

/**
 * @param {string} path as Git prints it: relative to the repository root, with `/`
 * @returns {{ kind: string, python: boolean, node: boolean }} which gates the file can affect
 */
export function classify(path) {
  const segments = path.split("/");
  if (path.startsWith("/") || segments.some((segment) => segment === "" || segment === "." || segment === "..")) {
    return { kind: "unreadable path", python: true, node: true };
  }
  const forced = EVERYTHING.find((entry) => entry.test(path));
  if (forced) return { kind: forced.name, python: true, node: true };
  const listed = KINDS.find((entry) => entry.test(path));
  if (listed) return { kind: listed.kind, python: listed.python, node: listed.node };
  return { kind: "unlisted", python: true, node: true };
}

/**
 * @param {string} event the GitHub event name
 * @param {ReadonlyArray<string>} paths every path the pull request adds, changes, removes or renames (both names)
 * @returns {{ python: boolean, node: boolean, reasons: string[] }}
 */
export function decide(event, paths) {
  if (event !== FILTERED_EVENT) {
    return { python: true, node: true, reasons: [`event "${event}" is not a pull request: every gate runs`] };
  }
  if (paths.length === 0) {
    return { python: true, node: true, reasons: ["no changed file was found: every gate runs"] };
  }
  const classified = paths.map((path) => ({ path, ...classify(path) }));
  const reasons = [];
  for (const gates of /** @type {const} */ (["python", "node"])) {
    const needing = classified.filter((file) => file[gates]);
    const shown = needing.slice(0, 5).map((file) => `${file.path} (${file.kind})`);
    if (needing.length > shown.length) shown.push(`and ${needing.length - shown.length} more`);
    reasons.push(
      needing.length === 0
        ? `${gates} gates skipped: none of the ${paths.length} changed files can affect them`
        : `${gates} gates run: ${shown.join(", ")}`,
    );
  }
  return {
    python: classified.some((file) => file.python),
    node: classified.some((file) => file.node),
    reasons,
  };
}

/** @param {ReadonlyArray<string>} argv @param {string} input NUL-separated paths @returns {number} exit code */
export function main(argv, input) {
  if (argv.length !== 2 || argv[0] !== "--event" || !argv[1]) {
    console.error("Usage: node scripts/ci/gates-for-changes.mjs --event <github event name> < NUL-separated paths");
    return 2;
  }
  const paths = input.split("\0").filter((path) => path !== "");
  const decision = decide(argv[1], paths);
  for (const reason of decision.reasons) console.error(`path rule: ${reason}`);
  console.log(`python=${decision.python}\nnode=${decision.node}`);
  return 0;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2), readFileSync(0, "utf8"));
}
