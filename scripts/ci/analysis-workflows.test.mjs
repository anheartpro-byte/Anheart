// What must stay true of .github/workflows/codeql.yml, the static analysis
// workflow, and of the file that sets its scope.
//
// Run by the `docs` job of ci.yml with the other CI test files, on
// every run and without any install: the workflow is read as text, like
// ci.yml is by ci-workflow.test.mjs. CodeQL itself cannot be run here: these
// tests hold what a later edit could break without any job turning red, and
// the scope is checked against the files of the repository as it is.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const WORKFLOWS = ".github/workflows";
const CODEQL_CONFIG = ".github/codeql/codeql-config.yml";

/** @param {string} path relative to the repository root */
const read = (path) => readFileSync(join(root, path), "utf8");
/** A workflow without its comment lines: what GitHub acts on. @param {string} file */
const workflow = (file) =>
  read(`${WORKFLOWS}/${file}`)
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("#"))
    .join("\n");

const CI = workflow("ci.yml");
const CODEQL = workflow("codeql.yml");

/**
 * Each job's own lines, by job id.
 * @param {string} text a workflow @returns {Map<string, string>}
 */
const jobsOf = (text) => {
  const body = text.slice(text.indexOf("\njobs:\n") + "\njobs:\n".length);
  const starts = [...body.matchAll(/^ {2}([a-z][a-z0-9-]*):[ \t]*$/gm)];
  return new Map(
    starts.map((start, index) => [start[1] ?? "", body.slice(start.index, starts[index + 1]?.index ?? body.length)]),
  );
};

/** @param {string} job the lines of one job @param {string} key a key of the job itself, not of one of its steps */
const own = (job, key) => new RegExp(`^ {4}${key}:[ \\t]*(.*)$`, "m").exec(job)?.[1];

/**
 * The lines nested under a key, trimmed.
 * @param {string} text @param {string} key the key, with its indentation
 */
const block = (text, key) => {
  const indent = /^ */.exec(key)?.[0].length ?? 0;
  const nested = new RegExp(`^${key}:[ \\t]*\\n((?: {${indent + 2}}.*\\n)+)`, "m").exec(text)?.[1] ?? "";
  return nested.split("\n").filter(Boolean).map((line) => line.trim());
};

/**
 * Every `uses:` of a workflow: the action, the commit it is pinned to, the version in the comment.
 * @param {string} text a workflow
 */
const actions = (text) =>
  [...text.matchAll(/^ +(?:- )?uses:[ \t]*(.*)$/gm)].map(([, written = ""]) => {
    const parts = /^([\w.-]+\/[\w./-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$/.exec(written);
    return { written, action: parts?.[1], pin: parts ? `${parts[2]} ${parts[3]}` : undefined };
  });

const JOBS = jobsOf(CODEQL);
const ANALYZE = JOBS.get("analyze") ?? "";
/** What comes before the jobs: name, triggers, permissions, concurrency. */
const HEAD = CODEQL.slice(0, CODEQL.indexOf("\njobs:\n"));

test("the CodeQL workflow has the one job this file knows about", () => {
  assert.deepEqual([...JOBS.keys()], ["analyze"]);
});

test("every action is pinned to a full commit, with its version in a comment", () => {
  const used = actions(CODEQL);
  assert.equal(used.length, 3);
  for (const { written, pin } of used) assert.ok(pin, `"uses: ${written}" is not <action>@<40 hex> # vX.Y.Z`);
  // An action ci.yml already uses is pinned to the same commit here: one version to review and to update.
  const reviewed = new Map(actions(CI).map(({ action, pin }) => [action, pin]));
  assert.ok(reviewed.has("actions/checkout"), "the actions of ci.yml were not read");
  assert.equal(used[0]?.action, "actions/checkout");
  for (const { action, pin } of used) if (reviewed.has(action)) assert.equal(pin, reviewed.get(action), action);
  // The two halves of the CodeQL action come from the same release.
  assert.deepEqual(
    used.slice(1).map(({ action }) => action),
    ["github/codeql-action/init", "github/codeql-action/analyze"],
  );
  assert.equal(used[1]?.pin, used[2]?.pin);
});

test("the analysis job cannot be taken for a job of ci.yml, required or not", () => {
  // Branch protection knows a check by its name alone: a job here that took
  // the id or the display name of a gate would report under a required name.
  const taken = new Set();
  for (const [id, job] of jobsOf(CI)) {
    taken.add(id);
    taken.add(own(job, "name") ?? id);
  }
  assert.ok(taken.has("pi-gate") && taken.has("docs"), "the jobs of ci.yml were not read");
  for (const [id, job] of JOBS) {
    for (const shown of [id, own(job, "name") ?? id]) assert.ok(!taken.has(shown), `"${shown}" is a job of ci.yml`);
  }
  // One check per language: under one fixed name, a language would hide the others.
  assert.equal(own(ANALYZE, "name"), "codeql (${{ matrix.language }})");
});

test("CodeQL starts on pull requests, on pushes to develop and main and once a week, never filtered by path", () => {
  assert.match(HEAD, /^ {2}pull_request:\n {4}branches: \[main, develop\]$/m);
  assert.match(HEAD, /^ {2}push:\n {4}branches: \[main, develop\]$/m);
  // Once a week: the queries change even when the code does not.
  assert.match(HEAD, /^ {2}schedule:\n {4}- cron: '\d+ \d+ \* \* [0-6]'$/m);
  assert.match(HEAD, /^ {2}workflow_dispatch:$/m);
  assert.doesNotMatch(HEAD, /paths(-ignore)?:/, "a filtered workflow would leave a required check pending");
  // The events that hand a write token to a run started by a fork.
  assert.doesNotMatch(HEAD, /pull_request_target|workflow_run/);
});

test("the token of each run can only read, except to publish the CodeQL results", () => {
  assert.deepEqual(block(CODEQL, "permissions"), ["contents: read"]);
  assert.deepEqual(block(ANALYZE, "    permissions"), ["contents: read", "security-events: write"]);
  // Counted over every workflow of the repository: one job writes, and only security events.
  const all = readdirSync(join(root, WORKFLOWS)).filter((file) => /\.ya?ml$/.test(file));
  assert.ok(all.includes("ci.yml") && all.includes("codeql.yml"), "the workflows were not listed");
  const everywhere = all.map(workflow).join("\n");
  assert.equal(everywhere.split(": write").length - 1, 1);
  assert.equal(everywhere.split("security-events").length - 1, 1);
  assert.doesNotMatch(everywhere, /write-all/);
  // No secret is read, and the checkout leaves no token in the Git configuration.
  assert.doesNotMatch(CODEQL, /secrets\./);
  assert.equal(CODEQL.split("actions/checkout@").length - 1, 1);
  assert.equal(CODEQL.split("persist-credentials: false").length - 1, 1);
});

test("CodeQL reads each language as it is, one job per language", () => {
  assert.match(ANALYZE, /^ {8}language: \[python, javascript-typescript, actions\]$/m);
  assert.match(ANALYZE, /^ {6}fail-fast: false$/m, "a language that fails must not cancel the others");
  const init = ANALYZE.slice(ANALYZE.indexOf("github/codeql-action/init@"), ANALYZE.indexOf("github/codeql-action/analyze@"));
  assert.match(init, /^ {10}languages: \$\{\{ matrix\.language \}\}$/m);
  assert.match(init, /^ {10}build-mode: none$/m);
  assert.ok(init.includes(`\n          config-file: ${CODEQL_CONFIG}\n`), "the scope file is not given to CodeQL");
  assert.ok(existsSync(join(root, CODEQL_CONFIG)), `${CODEQL_CONFIG} is missing`);
  assert.match(ANALYZE, /^ {10}category: \/language:\$\{\{ matrix\.language \}\}$/m);
  // Nothing of the repository is installed or run by this job.
  assert.doesNotMatch(ANALYZE, /^ +(- )?run:/m);
});

/** The entries of `paths-ignore` in the CodeQL configuration, unquoted. */
const ignored = () =>
  block(read(CODEQL_CONFIG), "paths-ignore")
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).replaceAll("'", ""));

/**
 * Whether an entry of `paths-ignore` leaves a file out. An entry names a
 * directory: at the root, or at any depth after a leading "**" and a slash,
 * with `*` for anything but a slash in a name.
 * @param {string} entry @param {string} path a file, relative to the repository root
 */
const leavesOut = (entry, path) => {
  const anywhere = entry.startsWith("**/");
  const name = (anywhere ? entry.slice(3) : entry)
    .split("*")
    .map((part) => part.replaceAll(/[.+?^${}()|[\]\\]/g, "\\$&"))
    .join("[^/]*");
  return new RegExp(`^${anywhere ? "(?:.*/)?" : ""}${name}/`).test(path);
};

test("CodeQL leaves out generated, installed and binary paths, and nothing else", () => {
  const entries = ignored();
  for (const expected of ["convex/_generated", "**/node_modules", ".next", "**/.venv", "**/.venv-*", "**/venv", "CAO"]) {
    assert.ok(entries.includes(expected), `${expected} is analysed`);
  }
  // No key that would narrow the analysis to some paths or to some queries.
  assert.doesNotMatch(read(CODEQL_CONFIG), /^(paths|disable-default-queries|query-filters):/m);
  // The one suite named is the widest one: security and quality, nothing else.
  assert.deepEqual(block(read(CODEQL_CONFIG), "queries"), ["- uses: security-and-quality"]);
  // The way this file reads an entry, on paths that exist only once something is installed or built.
  assert.ok(entries.some((entry) => leavesOut(entry, "node_modules/react/index.js")));
  assert.ok(entries.some((entry) => leavesOut(entry, "raspberry-pi/.venv/lib/python3.12/site.py")));
  assert.ok(entries.some((entry) => leavesOut(entry, "simulation/cad/.venv-cad/bin/activate_this.py")));
  assert.ok(!entries.some((entry) => leavesOut(entry, "raspberry-pi/src/venv_tools.py")));
  assert.ok(!entries.some((entry) => leavesOut(entry, "simulation/output.py")));
});

// The scope must hold for the repository as it is, not only for chosen entries.
const tracked = spawnSync("git", ["ls-files", "-z"], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
const files = tracked.status === 0 ? tracked.stdout.split("\0").filter(Boolean) : [];

test("of the tracked files, CodeQL leaves out the Convex bindings and the CAD file only", { skip: files.length === 0 }, () => {
  assert.ok(files.length > 500, "the tracked files were not listed");
  const entries = ignored();
  const left = files.filter((path) => entries.some((entry) => leavesOut(entry, path)));
  assert.ok(left.length > 0, "nothing is left out: the entries were not read");
  for (const path of left) assert.match(path, /^(convex\/_generated|CAO)\//, `${path} is left out of the analysis`);
  for (const path of files) {
    if (/^(convex\/_generated|CAO)\//.test(path)) assert.ok(left.includes(path), `${path} is analysed`);
  }
  for (const path of [
    "raspberry-pi/src/training/safety.py",
    "raspberry-pi/tests/web/panel_manual.test.mjs",
    "raspberry-pi/src/web/static/app.js",
    "simulation/harness.py",
    "convex/training.ts",
    "convex/lib/auth.ts",
    "app/[locale]/page.tsx",
    ".github/workflows/ci.yml",
    ".github/workflows/codeql.yml",
  ]) {
    assert.ok(files.includes(path), `${path} is not tracked any more: choose another example`);
    assert.ok(!left.includes(path), `${path} is left out of the analysis`);
  }
});
