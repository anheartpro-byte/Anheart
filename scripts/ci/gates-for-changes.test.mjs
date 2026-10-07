import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { classify, decide } from "./gates-for-changes.mjs";

const script = fileURLToPath(new URL("gates-for-changes.mjs", import.meta.url));
const root = fileURLToPath(new URL("../../", import.meta.url));

const EVERYTHING = { python: true, node: true };
const NOTHING = { python: false, node: false };
const PYTHON_ONLY = { python: true, node: false };
const NODE_ONLY = { python: false, node: true };

/** @param {string[]} paths @param {string} [event] */
const gates = (paths, event = "pull_request") => {
  const { python, node } = decide(event, paths);
  return { python, node };
};

const DOCS = ["docs/securite.md", "docs/guides/img/console-01-tableau-de-bord-repos.png", "README.md"];
const SITE = ["app/[locale]/page.tsx", "components/ui/button.tsx", "lib/ecg.ts", "messages/fr.json", "tsconfig.json"];
const CONVEX = ["convex/training.ts", "convex/training.test.ts", "convex/_generated/api.d.ts"];
const PYTHON = ["raspberry-pi/src/training/safety.py", "raspberry-pi/stubs/bitalino.pyi", "simulation/tests/test_battery.py"];

test("documentation alone runs neither the Python gates nor the site gates", () => {
  assert.deepEqual(gates(DOCS), NOTHING);
  assert.deepEqual(gates(["raspberry-pi/README.md", "simulation/SESSION_TESTS.md", "deploy/simulation-vercel/README.md"]), NOTHING);
});

test("the site and Convex run their own gates and spare the Python ones", () => {
  assert.deepEqual(gates(SITE), NODE_ONLY);
  assert.deepEqual(gates(CONVEX), NODE_ONLY);
  assert.deepEqual(gates([...SITE, ...CONVEX, ...DOCS]), NODE_ONLY);
});

test("what the unit tests of the site are run with belongs to the site", () => {
  // The document and the stand-ins of test-support/: no Python gate reads them.
  for (const path of ["test-support/dom.ts", "test-support/render.tsx", "test-support/convex.ts"]) {
    assert.deepEqual(classify(path), { kind: "site", ...NODE_ONLY }, path);
  }
  // Only that folder at the root: the same name elsewhere is a file of no listed kind.
  assert.deepEqual(gates(["raspberry-pi/test-support/dom.ts"]), EVERYTHING);
  assert.deepEqual(gates(["test-support.ts"]), EVERYTHING);
});

test("a Vitest configuration at the root belongs to the site, whatever its suite", () => {
  for (const path of [
    "vitest.convex.config.mts",
    "vitest.ecg.config.mts",
    "vitest.site.config.mts",
    "vitest.site-coverage.config.mts",
  ]) {
    assert.deepEqual(classify(path), { kind: "site", ...NODE_ONLY }, path);
  }
  for (const path of [
    "vitest.config.mts",
    "vitest.site.config.mts.orig",
    "vitest.SITE.config.mts",
    "vitest.site.config.ts",
    "raspberry-pi/vitest.site.config.mts",
    "simulation/vitest.site.config.mts",
  ]) {
    assert.deepEqual(gates([path]), EVERYTHING, path);
  }
});

test("the changelog is not documentation: the release tooling and its tests read it", () => {
  for (const path of ["CHANGELOG.md", "raspberry-pi/CHANGELOG.md", "docs/CHANGELOG.md", "Changelog.md"]) {
    assert.deepEqual(gates([path]), EVERYTHING, path);
    assert.deepEqual(gates([...DOCS, path]), EVERYTHING, path);
    assert.notEqual(classify(path).kind, "documentation", path);
  }
  // Only that name: other release pages are documentation like any page.
  assert.deepEqual(gates(["docs/release.md", "docs/CHANGELOG-notes.md", "NOT-A-CHANGELOG.md"]), NOTHING);
});

test("Python sources of the Pi and of the simulation run the Python gates and spare the site ones", () => {
  assert.deepEqual(gates(PYTHON), PYTHON_ONLY);
  assert.deepEqual(gates([...PYTHON, ...DOCS]), PYTHON_ONLY);
});

test("a pull request that touches both sides runs everything", () => {
  assert.deepEqual(gates([...SITE, ...PYTHON]), EVERYTHING);
  assert.deepEqual(gates([CONVEX[0], PYTHON[0]]), EVERYTHING);
});

test("one file of no listed kind runs everything, whatever stands next to it", () => {
  const unlisted = [
    "CAO/Gaura_Assy_2907.STEP",
    ".gitignore",
    ".gitleaks.toml",
    // Read by both Vercel projects, the site and the hosted simulation: not a file of the site alone.
    "vercel.json",
    ".cursor/rules/convex_rules.mdc",
    "simulation_app.py",
    "deploy/simulation-vercel/app.py",
    "raspberry-pi/config/motion_limits.json",
    "raspberry-pi/Dockerfile",
    "raspberry-pi/scripts/check.sh",
    "simulation/scenarios/auto_jog_150_nominal.json",
    "simulation/viewer/index.html",
    "docs/outil.mjs",
    "docs/donnees.json",
    "a-new-directory/file.txt",
  ];
  for (const path of unlisted) {
    assert.deepEqual(classify(path), { kind: "unlisted", ...EVERYTHING }, path);
    assert.deepEqual(gates([path]), EVERYTHING, path);
    assert.deepEqual(gates([...DOCS, path]), EVERYTHING, path);
    assert.deepEqual(gates([...SITE, path]), EVERYTHING, path);
    assert.deepEqual(gates([...PYTHON, path]), EVERYTHING, path);
  }
});

test("the CI itself, manifests, lockfiles and files tested on both sides run everything", () => {
  const forced = [
    ".github/workflows/ci.yml",
    ".github/PULL_REQUEST_TEMPLATE.md",
    "scripts/ci/gates-for-changes.mjs",
    "scripts/ci/pi_gate_shard.py",
    "scripts/ci/notes.md",
    "package.json",
    "package-lock.json",
    "bun.lock",
    "requirements.txt",
    "pyproject.toml",
    "raspberry-pi/pyproject.toml",
    "raspberry-pi/requirements-dev.txt",
    "simulation/pyproject.toml",
    "simulation/cad/requirements.txt",
    "deploy/simulation-vercel/requirements.txt",
    "raspberry-pi/src/web/static/app.js",
    "raspberry-pi/src/web/static/index.html",
    "raspberry-pi/src/web/static/notes.md",
    "raspberry-pi/tests/web/panel_manual.test.mjs",
    "contracts/machine-api.json",
    "contracts/README.md",
    "docs/pi-image.md",
  ];
  for (const path of forced) {
    const { kind, ...needed } = classify(path);
    assert.notEqual(kind, "unlisted", path);
    assert.deepEqual(needed, EVERYTHING, path);
    assert.deepEqual(gates([...DOCS, path]), EVERYTHING, path);
  }
});

test("names that only look like a listed directory are not listed", () => {
  for (const path of ["apps/page.tsx", "application.ts", "convex.ts", "library/x.ts", "raspberry-pi-old/x.py", "simulations/x.py", "x.py", "src/x.py", "docs.json"]) {
    assert.deepEqual(gates([path]), EVERYTHING, path);
  }
  // The extension must be the whole extension, and the case is not folded.
  for (const path of ["raspberry-pi/src/x.py.orig", "raspberry-pi/src/x.pyc", "simulation/x.PY", "docs/x.MD.exe", "docs/x.pngx"]) {
    assert.deepEqual(gates([path]), EVERYTHING, path);
  }
});

test("a path that is not a plain relative path runs everything", () => {
  for (const path of ["/docs/a.md", "docs/../raspberry-pi/a.md", "./docs/a.md", "docs//a.md", "docs/a.md/"]) {
    assert.deepEqual(classify(path), { kind: "unreadable path", ...EVERYTHING }, path);
  }
});

test("a rename counts under both names", () => {
  // git diff --no-renames lists the name removed and the name added.
  assert.deepEqual(gates(["docs/note.md", "raspberry-pi/src/note.py"]), PYTHON_ONLY);
  assert.deepEqual(gates(["raspberry-pi/src/panel.py", "app/panel.tsx"]), EVERYTHING);
  assert.deepEqual(gates(["docs/a.md", "docs/b.md"]), NOTHING);
  assert.deepEqual(gates(["raspberry-pi/config/profiles.default.json", "docs/profiles.md"]), EVERYTHING);
});

test("a removed file counts like a changed one", () => {
  // The list holds names only: nothing tells a removal from an edit, on purpose.
  assert.deepEqual(gates(["raspberry-pi/tests/test_safety.py"]), PYTHON_ONLY);
  assert.deepEqual(gates(["convex/training.test.ts"]), NODE_ONLY);
  assert.deepEqual(gates(["simulation/scenarios/estop_auto.json"]), EVERYTHING);
});

test("a pull request with no changed file runs everything", () => {
  assert.deepEqual(gates([]), EVERYTHING);
});

test("anything but a pull request runs everything, even for documentation alone", () => {
  for (const event of ["push", "schedule", "workflow_dispatch", "merge_group", "pull_request_target", ""]) {
    assert.deepEqual(gates(DOCS, event), EVERYTHING, event);
    assert.deepEqual(gates([], event), EVERYTHING, event);
  }
});

test("the reasons name the files that keep a gate", () => {
  const { reasons } = decide("pull_request", [...DOCS, "raspberry-pi/src/units.py", "raspberry-pi/Dockerfile"]);
  assert.deepEqual(reasons, [
    "python gates run: raspberry-pi/src/units.py (python), raspberry-pi/Dockerfile (unlisted)",
    "node gates run: raspberry-pi/Dockerfile (unlisted)",
  ]);
  assert.deepEqual(decide("pull_request", DOCS).reasons, [
    "python gates skipped: none of the 3 changed files can affect them",
    "node gates skipped: none of the 3 changed files can affect them",
  ]);
});

/** @param {string[]} args @param {string} input */
const run = (args, input) => spawnSync(process.execPath, [script, ...args], { input, encoding: "utf8" });

test("the command reads NUL-separated paths and prints the two outputs", () => {
  const docs = run(["--event", "pull_request"], "docs/a.md\0docs/un nom avec espace.md\0");
  assert.equal(docs.status, 0, docs.stderr);
  assert.equal(docs.stdout, "python=false\nnode=false\n");
  assert.match(docs.stderr, /python gates skipped: none of the 2 changed files/);

  const mixed = run(["--event", "pull_request"], "docs/a.md\0raspberry-pi/src/a\nb.py\0");
  assert.equal(mixed.stdout, "python=true\nnode=false\n");

  // A newline is not a separator: this is ONE unlisted path, not two documents.
  assert.equal(run(["--event", "pull_request"], "docs/a.md\ndocs/b.txt").stdout, "python=true\nnode=true\n");
  assert.equal(run(["--event", "pull_request"], "").stdout, "python=true\nnode=true\n");
  assert.equal(run(["--event", "push"], "docs/a.md\0").stdout, "python=true\nnode=true\n");
});

test("the command refuses to guess when it is not told the event", () => {
  for (const args of [[], ["--event"], ["pull_request"], ["--event", "pull_request", "extra"], ["--event", ""]]) {
    const result = run(args, "docs/a.md\0");
    assert.equal(result.status, 2, JSON.stringify(args));
    assert.equal(result.stdout, "", JSON.stringify(args));
  }
});

// The lists must hold for the repository as it is, not only for chosen examples.
const tracked = spawnSync("git", ["ls-files", "-z"], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
const files = tracked.status === 0 ? tracked.stdout.split("\0").filter(Boolean) : [];

test("every tracked file the site tooling reads keeps the site gates", { skip: files.length === 0 }, () => {
  // TypeScript (tsconfig "include"), ESLint (JavaScript and TypeScript, the
  // whole repository) and the node tests of the console.
  const read = files.filter(
    (path) =>
      /\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/.test(path) ||
      path.startsWith("raspberry-pi/src/web/static/") ||
      path.startsWith("raspberry-pi/tests/web/"),
  );
  assert.ok(read.length > 100, "the tracked files were not listed");
  for (const path of read) assert.equal(classify(path).node, true, path);
});

test("every tracked file under the Pi, the simulation and the CI keeps the Python gates", { skip: files.length === 0 }, () => {
  const read = files.filter(
    (path) => /^(raspberry-pi|simulation|scripts\/ci|\.github|CAO)\//.test(path) && !path.endsWith(".md"),
  );
  assert.ok(read.length > 300, "the tracked files were not listed");
  for (const path of read) assert.equal(classify(path).python, true, path);
});

test("no tracked file is skipped by a kind it does not belong to", { skip: files.length === 0 }, () => {
  for (const path of files) {
    const { kind } = classify(path);
    if (kind === "python") assert.match(path, /^(raspberry-pi|simulation)\/.*\.pyi?$/, path);
    if (kind === "site") assert.doesNotMatch(path, /\.(py|pyi)$/, path);
    if (kind === "convex") assert.match(path, /^convex\//, path);
    if (kind === "documentation") assert.match(path, /(\.md$|^docs\/)/, path);
  }
});
