// What must stay true of the two static analysis workflows, codeql.yml and
// sonar.yml, and of the files that set their scope.
//
// Run by the `sonar-config` job of sonar.yml before it decides anything,
// without any install: the workflows are read as text, like ci.yml is by
// ci-workflow.test.mjs. A scanner cannot be run here (CodeQL needs GitHub,
// SonarQube Cloud needs its token): these tests hold what a later edit could
// break without any job turning red, and the scopes are checked against the
// files of the repository as it is.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
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
const SONAR = workflow("sonar.yml");
/** The two workflows this file is about, by file name. */
const ANALYSES = new Map([
  ["codeql.yml", CODEQL],
  ["sonar.yml", SONAR],
]);

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

/** What comes before the jobs: name, triggers, permissions, concurrency. @param {string} text a workflow */
const head = (text) => text.slice(0, text.indexOf("\njobs:\n"));

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

const CODEQL_JOBS = jobsOf(CODEQL);
const SONAR_JOBS = jobsOf(SONAR);
const ANALYZE = CODEQL_JOBS.get("analyze") ?? "";
const SONAR_CONFIG = SONAR_JOBS.get("sonar-config") ?? "";
const SONAR_SCAN = SONAR_JOBS.get("sonar") ?? "";

// ---------------------------------------------------------------- both workflows

test("the analysis workflows have the jobs this file knows about", () => {
  assert.deepEqual([...CODEQL_JOBS.keys()], ["analyze"]);
  assert.deepEqual([...SONAR_JOBS.keys()], ["sonar-config", "sonar"]);
});

test("every action is pinned to a full commit, with its version in a comment", () => {
  for (const [file, text] of ANALYSES) {
    const used = actions(text);
    assert.ok(used.length >= 2, `${file}: no action found`);
    for (const { written, pin } of used) assert.ok(pin, `${file}: "uses: ${written}" is not <action>@<40 hex> # vX.Y.Z`);
  }
  // An action ci.yml already uses is pinned to the same commit here: one version to review and to update.
  const reviewed = new Map(actions(CI).map(({ action, pin }) => [action, pin]));
  assert.ok(reviewed.has("actions/checkout"), "the actions of ci.yml were not read");
  for (const [file, text] of ANALYSES) {
    for (const { action, pin } of actions(text)) {
      if (reviewed.has(action)) assert.equal(pin, reviewed.get(action), `${file}: ${action}`);
    }
  }
  // The two halves of the CodeQL action come from the same release.
  const codeql = actions(CODEQL).filter(({ action }) => action?.startsWith("github/codeql-action/"));
  assert.deepEqual(
    codeql.map(({ action }) => action),
    ["github/codeql-action/init", "github/codeql-action/analyze"],
  );
  assert.equal(codeql[0]?.pin, codeql[1]?.pin);
});

test("no analysis job can be taken for a job of ci.yml, required or not", () => {
  // Branch protection knows a check by its name alone: a job here that took
  // the id or the display name of a gate would report under a required name.
  const taken = new Set();
  for (const [id, job] of jobsOf(CI)) {
    taken.add(id);
    taken.add(own(job, "name") ?? id);
  }
  assert.ok(taken.has("pi-gate") && taken.has("docs"), "the jobs of ci.yml were not read");
  const seen = new Set();
  for (const [file, text] of ANALYSES) {
    for (const [id, job] of jobsOf(text)) {
      for (const shown of new Set([id, own(job, "name") ?? id])) {
        assert.ok(!taken.has(shown), `${file}: "${shown}" is a job of ci.yml`);
        assert.ok(!seen.has(shown), `"${shown}" names two analysis jobs`);
        seen.add(shown);
      }
    }
  }
  // One check per language: under one fixed name, a language would hide the other.
  assert.equal(own(ANALYZE, "name"), "codeql (${{ matrix.language }})");
});

test("the analyses start on pull requests and on pushes to develop and main, never filtered by path", () => {
  for (const [file, text] of ANALYSES) {
    const triggers = head(text);
    assert.match(triggers, /^ {2}pull_request:\n {4}branches: \[main, develop\]$/m, file);
    assert.match(triggers, /^ {2}push:\n {4}branches: \[main, develop\]$/m, file);
    assert.match(triggers, /^ {2}workflow_dispatch:$/m, file);
    assert.doesNotMatch(triggers, /paths(-ignore)?:/, `${file}: a filtered workflow would leave a required check pending`);
    // The events that hand a secret or a write token to a run started by a fork.
    assert.doesNotMatch(triggers, /pull_request_target|workflow_run/, file);
  }
  // Once a week for CodeQL: its queries change even when the code does not.
  assert.match(head(CODEQL), /^ {2}schedule:\n {4}- cron: '\d+ \d+ \* \* [0-6]'$/m);
});

test("the token of each run can only read, except to publish the CodeQL results", () => {
  for (const [file, text] of ANALYSES) assert.deepEqual(block(text, "permissions"), ["contents: read"], file);
  assert.deepEqual(block(ANALYZE, "    permissions"), ["contents: read", "security-events: write"]);
  for (const [id, job] of SONAR_JOBS) assert.equal(own(job, "permissions"), undefined, id);
  // Counted over every workflow of the repository: one job writes, and only security events.
  const all = readdirSync(join(root, WORKFLOWS)).filter((file) => /\.ya?ml$/.test(file));
  assert.ok(all.includes("ci.yml") && all.length >= 3, "the workflows were not listed");
  const everywhere = all.map(workflow).join("\n");
  assert.equal(everywhere.split(": write").length - 1, 1);
  assert.equal(everywhere.split("security-events").length - 1, 1);
  assert.doesNotMatch(everywhere, /write-all/);
  // No checkout leaves the token of the run in the Git configuration.
  for (const [file, text] of ANALYSES) {
    const checkouts = text.split("actions/checkout@").length - 1;
    assert.ok(checkouts >= 1, file);
    assert.equal(text.split("persist-credentials: false").length - 1, checkouts, file);
  }
});

// ---------------------------------------------------------------- CodeQL

/** The entries of `paths-ignore` in the CodeQL configuration, unquoted. */
const codeqlIgnored = () =>
  block(read(CODEQL_CONFIG), "paths-ignore")
    .filter((line) => line.startsWith("- "))
    .map((line) => line.slice(2).replaceAll("'", ""));

test("CodeQL reads Python and JavaScript/TypeScript as they are, one job per language", () => {
  assert.match(ANALYZE, /^ {8}language: \[python, javascript-typescript\]$/m);
  assert.match(ANALYZE, /^ {6}fail-fast: false$/m, "a language that fails must not cancel the other");
  const init = ANALYZE.slice(ANALYZE.indexOf("github/codeql-action/init@"), ANALYZE.indexOf("github/codeql-action/analyze@"));
  assert.match(init, /^ {10}languages: \$\{\{ matrix\.language \}\}$/m);
  assert.match(init, /^ {10}build-mode: none$/m);
  assert.ok(init.includes(`\n          config-file: ${CODEQL_CONFIG}\n`), "the scope file is not given to CodeQL");
  assert.ok(existsSync(join(root, CODEQL_CONFIG)), `${CODEQL_CONFIG} is missing`);
  assert.match(ANALYZE, /^ {10}category: \/language:\$\{\{ matrix\.language \}\}$/m);
  // Nothing of the repository is installed or run by this job.
  assert.doesNotMatch(ANALYZE, /^ +(- )?run:/m);
});

test("CodeQL leaves out generated, installed and binary paths, and nothing else", () => {
  const ignored = codeqlIgnored();
  for (const expected of ["convex/_generated", "**/node_modules", ".next", "**/.venv", "**/.venv-*", "**/venv", "CAO"]) {
    assert.ok(ignored.includes(expected), `${expected} is analysed`);
  }
  // No key that would narrow the analysis to some paths or to some queries.
  assert.doesNotMatch(read(CODEQL_CONFIG), /^(paths|queries|disable-default-queries|query-filters):/m);
  // No directory of code written here is one of the entries.
  for (const kept of ["raspberry-pi", "simulation", "convex", "app", "components", "hooks", "lib", "scripts", "deploy"]) {
    assert.ok(!ignored.some((entry) => entry.replace(/^\*\*\//, "") === kept), `${kept} is left out`);
  }
});

// ---------------------------------------------------------------- SonarQube Cloud

const TOKEN_STEP = "Say whether this run was given the SonarQube Cloud token";

/**
 * Runs the step of `sonar-config` that looks for the token, as it is written in the workflow.
 * @param {string | undefined} token the secret as the runner hands it: empty when it does not exist
 */
const tokenStep = (token) => {
  const step = SONAR_CONFIG.split(/^ {6}- /m).find((text) => text.startsWith(`name: ${TOKEN_STEP}\n`)) ?? "";
  const script = /^ {8}run: \|\n((?: {10}.*\n|\n)+)/m.exec(step)?.[1]?.replaceAll(/^ {10}/gm, "");
  assert.ok(script, `sonar-config: no step "${TOKEN_STEP}" with a run block`);
  const directory = mkdtempSync(join(tmpdir(), "anheart-sonar-token-"));
  try {
    const env = {
      PATH: process.env.PATH ?? "",
      GITHUB_OUTPUT: join(directory, "output"),
      GITHUB_STEP_SUMMARY: join(directory, "summary"),
    };
    const result = spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script], {
      env: token === undefined ? env : { ...env, SONAR_TOKEN: token },
      encoding: "utf8",
    });
    const written = (/** @type {string} */ file) => (existsSync(file) ? readFileSync(file, "utf8") : "");
    return {
      status: result.status,
      said: result.stdout + result.stderr,
      output: written(env.GITHUB_OUTPUT),
      summary: written(env.GITHUB_STEP_SUMMARY),
    };
  } finally {
    rmSync(directory, { recursive: true });
  }
};

test("without the token the run says so, the analysis is skipped and nothing fails", () => {
  for (const absent of ["", undefined]) {
    const { status, said, output, summary } = tokenStep(absent);
    assert.equal(status, 0, said);
    assert.equal(output, "present=false\n");
    assert.match(summary, /SONAR_TOKEN is not set .* skipped and nothing fails/);
  }
  const { status, said, output, summary } = tokenStep("sonar-word");
  assert.equal(status, 0, said);
  assert.equal(output, "present=true\n");
  assert.equal(summary, "");
  assert.doesNotMatch(said, /sonar-word/, "the step printed the token");
});

test("the analysis job runs only with the token, and never for a pull request from a fork", () => {
  assert.equal(own(SONAR_SCAN, "needs"), "sonar-config");
  assert.equal(
    own(SONAR_SCAN, "if"),
    "${{ needs.sonar-config.outputs.token == 'true' && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository) }}",
  );
  assert.match(SONAR_CONFIG, /^ {6}token: \$\{\{ steps\.token\.outputs\.present \}\}$/m);
  // The job that decides runs on every event, token or not.
  assert.equal(own(SONAR_CONFIG, "if"), undefined);
  assert.equal(own(SONAR_CONFIG, "needs"), undefined);
  // GitHub gives no secret to an `if`: none tries to read one.
  for (const [file, text] of ANALYSES) {
    for (const [, condition = ""] of text.matchAll(/^ +(?:- )?if:(.*)$/gm)) assert.doesNotMatch(condition, /secrets\./, file);
  }
  // The token is read in the environment of two steps, and no other secret anywhere.
  assert.doesNotMatch(CODEQL, /secrets\./);
  assert.deepEqual(
    [...SONAR.matchAll(/^.*secrets\..*$/gm)].map(([line]) => line.trim()),
    ["SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}", "SONAR_TOKEN: ${{ secrets.SONAR_TOKEN }}"],
  );
  // In the job that decides, only the step tested above is given it: not the step that runs code of the repository.
  const given = SONAR_CONFIG.split(/^ {6}- /m).filter((step) => step.includes("secrets."));
  assert.equal(given.length, 1);
  assert.ok(given[0]?.startsWith(`name: ${TOKEN_STEP}\n`));
  // The analysis is the official action, on the whole history.
  assert.match(SONAR_SCAN, /^ {8}uses: SonarSource\/sonarqube-scan-action@/m);
  assert.match(SONAR_SCAN, /^ {10}fetch-depth: 0$/m);
});

/**
 * sonar-project.properties as the scanner reads it: `key=value`, a value
 * continued on the next line after a final backslash, comments on their own lines.
 * @returns {Map<string, string>}
 */
const properties = () => {
  const found = new Map();
  for (const line of read("sonar-project.properties").replaceAll(/\\\n[ \t]*/g, "").split("\n")) {
    if (line.trim() === "" || line.startsWith("#")) continue;
    const separator = line.indexOf("=");
    assert.ok(separator > 0, `not a property: ${line}`);
    const key = line.slice(0, separator).trim();
    assert.ok(!found.has(key), `${key} is set twice`);
    found.set(key, line.slice(separator + 1).trim());
  }
  return found;
};

/** @param {string} key @returns {string[]} the comma-separated value of a property */
const listed = (key) =>
  (properties().get(key) ?? "")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

/**
 * A path pattern of SonarQube: `**` any directories, `*` anything but a slash, `?` one character.
 * @param {string} written @returns {RegExp}
 */
const glob = (written) =>
  new RegExp(
    `^${written
      .split(/(\*\*\/|\*\*|\*|\?)/)
      .map((part) => ({ "**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]" })[part] ?? part.replaceAll(/[.+^${}()|[\]\\]/g, "\\$&"))
      .join("")}$`,
  );

/** @param {string} key a property holding patterns @returns {(path: string) => boolean} */
const matcher = (key) => {
  const patterns = listed(key).map(glob);
  return (path) => patterns.some((expression) => expression.test(path));
};

test("the keys of the organization and of the project are written once, in sonar-project.properties", () => {
  const set = properties();
  assert.equal(set.get("sonar.organization"), "anheartpro-byte");
  assert.equal(set.get("sonar.projectKey"), "anheartpro-byte_Anheart");
  // Not a second time in the workflow, where an argument would silently win over the file.
  assert.doesNotMatch(SONAR, /sonar\.(organization|projectKey)|-Dsonar\.|^ +args:|anheartpro-byte/m);
});

test("a failed quality gate does not fail the analysis job", () => {
  // Whether the analyses block anything is a setting of the repository,
  // decided by the product owner: nothing here may decide it first.
  assert.equal(properties().get("sonar.qualitygate.wait"), undefined);
  assert.doesNotMatch(SONAR, /qualitygate/);
});

test("SonarQube Cloud is never left to show 0 % of coverage for want of a report", () => {
  const set = properties();
  const imported = [...set.keys()].some((key) => /coverage\.reportPaths?$|lcov\.reportPaths$/.test(key));
  assert.ok(imported || set.get("sonar.coverage.exclusions") === "**/*", "no report is imported, yet files are counted for coverage");
});

test("the scope of SonarQube Cloud is the whole repository, less what is listed", () => {
  const set = properties();
  assert.equal(set.get("sonar.sources"), ".");
  assert.equal(set.get("sonar.tests"), ".");
  assert.equal(set.get("sonar.inclusions"), undefined, "an inclusion list would leave new directories out");
  const excluded = listed("sonar.exclusions");
  for (const expected of [
    "**/node_modules/**",
    ".next/**",
    "convex/_generated/**",
    "**/.venv/**",
    "**/.venv-*/**",
    "**/venv/**",
    "CAO/**",
    "docs/**/img/**",
    "simulation/scenarios/real/**",
  ]) {
    assert.ok(excluded.includes(expected), `${expected} is analysed`);
  }
  // A file is either source or test: every pattern that declares tests also takes them out of the sources.
  const declared = listed("sonar.test.inclusions");
  assert.ok(declared.length > 0, "no test code is declared");
  for (const entry of declared) assert.ok(excluded.includes(entry), `${entry} is both source and test`);
  // What CodeQL leaves out, SonarQube Cloud leaves out too: one scope to explain.
  for (const entry of codeqlIgnored()) assert.ok(excluded.includes(`${entry}/**`), `CodeQL leaves ${entry} out, SonarQube Cloud does not`);
});

// The scope must hold for the repository as it is, not only for chosen patterns.
const tracked = spawnSync("git", ["ls-files", "-z"], { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024 });
const files = tracked.status === 0 ? tracked.stdout.split("\0").filter(Boolean) : [];

test("every tracked file is source, test or left out for a listed reason", { skip: files.length === 0 }, () => {
  assert.ok(files.length > 500, "the tracked files were not listed");
  const isTest = matcher("sonar.test.inclusions");
  const isExcluded = matcher("sonar.exclusions");
  /** What pytest, Vitest and `node --test` collect. @param {string} path */
  const collected = (path) =>
    /(^|\/)tests\//.test(path) || /\.test\.(ts|tsx|mts|mjs)$/.test(path) || /^scripts\/ci\/test_[^/]*\.py$/.test(path);

  // Test code: every collected file, the fixtures the Convex tests share, and nothing else.
  const tests = files.filter(isTest);
  assert.ok(tests.length > 120, `only ${tests.length} test files`);
  for (const path of files) if (collected(path)) assert.ok(isTest(path), `${path} is a test analysed as source`);
  for (const path of tests) {
    assert.ok(collected(path) || /^convex\/(test\.setup|[^/]+\.fixtures)\.ts$/.test(path), `${path} is source analysed as a test`);
  }

  // Source code: everything written here stays in.
  const written = files.filter(
    (path) => /\.(py|pyi|ts|tsx|mts|mjs|js|html|css|sh)$/.test(path) && !isTest(path) && !path.startsWith("convex/_generated/"),
  );
  assert.ok(written.length > 200, `only ${written.length} source files`);
  for (const path of written) assert.ok(!isExcluded(path), `${path} is left out of the analysis`);
  for (const path of [
    "raspberry-pi/src/training/safety.py",
    "convex/training.ts",
    "convex/lib/auth.ts",
    "raspberry-pi/src/web/static/app.js",
    "simulation/harness.py",
    "app/[locale]/page.tsx",
  ]) {
    assert.ok(written.includes(path), `${path} is not an analysed source any more`);
  }

  // Left out: the generated bindings, the CAD file, the screenshots, the lockfiles.
  for (const path of files) {
    if (/^(convex\/_generated|CAO|docs\/guides\/img)\//.test(path) || path === "package-lock.json" || path === "bun.lock") {
      assert.ok(isExcluded(path), `${path} is analysed`);
    }
  }
});
