#!/usr/bin/env node
// The quality report of a CI run (docs/framework-de-test.md, section CI,
// "Rapport de qualité").
//
// Two commands, both run by .github/workflows/ci.yml, without any install:
//
//   job <part> --dir <dir> ...   in a gate job, after the gate. Reads what the
//       tools of that job left for machines in <dir> (JUnit XML, coverage
//       JSON, how each stage ended), writes the job's own summary and the
//       numbers of the job, <dir>/part.json.
//   report --artifacts <dir> --out <dir>   in the last job of the run. Reads
//       the numbers of every job and how each job ended, writes the table of
//       the run and <out>/quality-report.json, and says the essentials again
//       as a notice (an annotation of the run).
//
// What this file holds to:
// - a number comes from a file a tool wrote for machines, never from a log;
// - nothing is filled in: a gate the path rule skipped is "sautée", a job that
//   left no numbers is "indisponible", what no tool measures is "non mesurée";
// - it never decides a job: whatever goes wrong the exit code is 0, and the
//   summary says "rapport indisponible";
// - the summary of a public repository is public. It holds names, counts and
//   durations: no message and no output of a test, and nothing of what the
//   audit or the static analysis found.

import { appendFileSync, existsSync, mkdirSync, readdirSync, readFileSync, writeFileSync } from "node:fs";
import { join, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";

/** The version of `quality-report.json` and of `part.json`. Raised when a field changes meaning or goes. */
export const SCHEMA_VERSION = 1;

/** How many files and tests a summary lists. */
const LISTED = 10;

/** From how many seconds a test is listed among the slowest of its suite. */
const SLOW_SECONDS = 0.1;

/**
 * The lines of the table, with the jobs whose verdict is shown on each line.
 * @type {readonly {id: string, label: string, gates: readonly string[]}[]}
 */
export const PROJECTS = [
  { id: "pi", label: "Console du Pi", gates: ["pi-gate"] },
  { id: "simulation", label: "Simulation", gates: ["simulation-gate"] },
  { id: "convex", label: "Convex", gates: ["convex-tests"] },
  { id: "site", label: "Site (`lib/`, `hooks/`, `components/`)", gates: ["web"] },
  { id: "scripts", label: "Scripts (CI et release)", gates: ["changes", "audit", "docs"] },
];

/** The checks branch protection requires, in the order of the workflow. */
export const GATES = ["pi-gate", "simulation-gate", "convex-tests", "web", "audit", "docs"];

/**
 * The answer of the path rule each job waits for; a job that is not here always runs.
 * @type {Readonly<Record<string, string>>}
 */
const RULE = { "pi-gate": "python", "simulation-gate": "python", "convex-tests": "node", web: "node" };

/**
 * Every test suite the CI runs: the project it counts for, the job that runs
 * it, the artifact (`quality-<part>`) its numbers arrive in.
 * @type {readonly {id: string, project: string, job: string, part: string, label: string, runner: string}[]}
 */
export const SUITES = [
  { id: "pi-pytest", project: "pi", job: "pi-gate", part: "pi", label: "tests Python de la console", runner: "pytest" },
  {
    id: "pi-panel",
    project: "pi",
    job: "web",
    part: "site",
    label: "panneau local, JavaScript",
    runner: "node --test",
  },
  {
    id: "simulation-battery",
    project: "simulation",
    job: "simulation-gate",
    part: "simulation",
    label: "batterie de scénarios",
    runner: "pytest",
  },
  { id: "convex", project: "convex", job: "convex-tests", part: "convex", label: "fonctions Convex", runner: "vitest" },
  { id: "site-lib", project: "site", job: "web", part: "site", label: "`lib/`", runner: "vitest" },
  {
    id: "site-components",
    project: "site",
    job: "web",
    part: "site",
    label: "`hooks/` et `components/`",
    runner: "vitest",
  },
  {
    id: "scripts-ci",
    project: "scripts",
    job: "changes",
    part: "scripts-changes",
    label: "règles de la CI et du rapport",
    runner: "node --test",
  },
  {
    id: "scripts-dependency-guard",
    project: "scripts",
    job: "audit",
    part: "scripts-audit",
    label: "garde d'une dépendance",
    runner: "node --test",
  },
  {
    id: "scripts-gitleaks-fixture",
    project: "scripts",
    job: "audit",
    part: "scripts-audit",
    label: "exception de la clé publique de test",
    runner: "node --test",
  },
  {
    id: "scripts-men",
    project: "scripts",
    job: "docs",
    part: "scripts-docs",
    label: "identifiants de menaces",
    runner: "node --test",
  },
  {
    id: "scripts-gate-runner",
    project: "scripts",
    job: "pi-gate",
    part: "pi",
    label: "lanceur des gates Python",
    runner: "pytest",
  },
];

/**
 * Every coverage measure of the CI. `main` is the one the table shows for its
 * project; `threshold` is the percentage its gate requires, when it requires one.
 * @type {readonly {id: string, project: string, job: string, part: string, label: string, main: boolean,
 *   threshold?: number}[]}
 */
export const COVERAGES = [
  { id: "pi", project: "pi", job: "pi-gate", part: "pi", label: "tout `raspberry-pi/src/`", main: true },
  {
    id: "pi-safety-chain",
    project: "pi",
    job: "pi-gate",
    part: "pi",
    label: "chaîne de sécurité du Pi",
    main: false,
    threshold: 100,
  },
  {
    id: "simulation",
    project: "simulation",
    job: "simulation-gate",
    part: "simulation",
    label: "code de la simulation",
    main: true,
    threshold: 100,
  },
  { id: "convex", project: "convex", job: "convex-tests", part: "convex", label: "`convex/`", main: true },
  { id: "site", project: "site", job: "web", part: "site", label: "`lib/`, `hooks/`, `components/`", main: true },
];

/**
 * The jobs the lint and the type checks of each project come from. The
 * scripts are read by ESLint with the site and by ruff with the Pi; only their
 * Python files have a type check.
 * @type {Readonly<Record<string, {lint: readonly string[], types: readonly string[]}>>}
 */
export const CHECK_JOBS = {
  pi: { lint: ["pi-gate"], types: ["pi-gate"] },
  simulation: { lint: ["simulation-gate"], types: ["simulation-gate"] },
  convex: { lint: ["web"], types: ["convex-tests"] },
  site: { lint: ["web"], types: ["web"] },
  scripts: { lint: ["web", "pi-gate"], types: ["pi-gate"] },
};

// --- Reading files -----------------------------------------------------------

/** @param {string} path @returns {string | undefined} the text of a file, `undefined` if it cannot be read */
function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return undefined;
  }
}

/** @param {string} path @returns {unknown} the JSON value of a file, `undefined` if it is missing or is not JSON */
function readJson(path) {
  const text = readText(path);
  if (text === undefined) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

/** @param {string} directory @returns {string[]} the names in a directory, sorted; none if it is not there */
function namesIn(directory) {
  try {
    return readdirSync(directory).sort();
  } catch {
    return [];
  }
}

/** @param {unknown} value @returns {Record<string, unknown>} the value if it is a JSON object, else an empty one */
const object = (value) =>
  typeof value === "object" && value !== null && !Array.isArray(value)
    ? /** @type {Record<string, unknown>} */ (value)
    : {};

/** @param {unknown} value @returns {number | undefined} a count: a whole number, zero or more */
const count = (value) => (typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : undefined);

/** @param {number} value @param {number} digits */
const round = (value, digits) => Number(value.toFixed(digits));

// --- JUnit XML ---------------------------------------------------------------

/** A comment, a CDATA section, a declaration, or a tag with its attributes. */
const XML_TOKEN =
  /<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<\?[\s\S]*?\?>|<(\/?)([A-Za-z_][\w.:-]*)((?:\s+[\w.:-]+\s*=\s*(?:"[^"]*"|'[^']*'))*)\s*(\/?)>/g;
const XML_ATTRIBUTE = /([\w.:-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
/** @type {Readonly<Record<string, string>>} */
const XML_ENTITIES = { lt: "<", gt: ">", amp: "&", quot: '"', apos: "'" };

/** @param {string} text an attribute value as written @returns {string} */
const unescapeXml = (text) =>
  text.replaceAll(/&(#x[0-9a-fA-F]+|#[0-9]+|[a-z]+);/g, (whole, /** @type {string} */ name) => {
    if (name.startsWith("#x")) return String.fromCodePoint(Number.parseInt(name.slice(2), 16));
    if (name.startsWith("#")) return String.fromCodePoint(Number.parseInt(name.slice(1), 10));
    return XML_ENTITIES[name] ?? whole;
  });

/** @param {string} written the attributes of a tag as written @returns {Record<string, string>} */
function attributesOf(written) {
  /** @type {Record<string, string>} */
  const attributes = {};
  for (const [, name = "", double, single] of written.matchAll(XML_ATTRIBUTE)) {
    attributes[name] = unescapeXml(double ?? single ?? "");
  }
  return attributes;
}

/**
 * @typedef {{name: string, seconds: number, state: "passed" | "failed" | "skipped", expectedFailure: boolean}} TestCase
 */

/**
 * The test cases of one JUnit file, as pytest, vitest and `node --test` write
 * it. Only the names, the times and the verdicts are read: the text of a
 * failure and the output of a test stay in the file.
 * @param {string} xml @returns {TestCase[] | undefined} `undefined` if this is not a JUnit file
 */
export function parseJunit(xml) {
  /** @type {TestCase[]} */
  const cases = [];
  /** @type {TestCase | undefined} */
  let current;
  let seen = false;
  for (const [, closing, tag, written = "", selfClosing] of xml.matchAll(XML_TOKEN)) {
    if (tag === undefined) continue;
    if (tag === "testsuites" || tag === "testsuite") seen = true;
    if (tag === "testcase" && !closing) {
      const attributes = attributesOf(written);
      const group = attributes.classname ?? "";
      const seconds = Number(attributes.time);
      current = {
        // `node --test` gives every test the class "test": it says nothing.
        name: group === "" || group === "test" ? (attributes.name ?? "") : `${group} :: ${attributes.name ?? ""}`,
        seconds: Number.isFinite(seconds) && seconds > 0 ? seconds : 0,
        state: "passed",
        expectedFailure: false,
      };
    } else if (current !== undefined && !closing && (tag === "failure" || tag === "error")) {
      current.state = "failed";
    } else if (current !== undefined && !closing && tag === "skipped" && current.state !== "failed") {
      current.state = "skipped";
      current.expectedFailure = (attributesOf(written).type ?? "").includes("xfail");
    }
    if (current !== undefined && tag === "testcase" && (closing || selfClosing)) {
      cases.push(current);
      current = undefined;
    }
  }
  return seen ? cases : undefined;
}

/**
 * @typedef {{tests: number, passed: number, failed: number, skipped: number, expected_failures: number,
 *   duration_s: number, files: number, slowest: {name: string, seconds: number}[], failed_tests: string[]}} SuiteNumbers
 */

/**
 * The numbers of a suite from the JUnit files its processes wrote.
 * @param {readonly string[]} paths
 * @param {number} [expected] how many processes ran the suite: with fewer files the counts would be too low
 * @returns {SuiteNumbers | undefined} `undefined` unless every file is there and readable
 */
export function suiteNumbers(paths, expected) {
  if (paths.length === 0 || (expected !== undefined && paths.length !== expected)) return undefined;
  /** @type {TestCase[]} */
  const cases = [];
  for (const path of paths) {
    const text = readText(path);
    const parsed = text === undefined ? undefined : parseJunit(text);
    if (parsed === undefined) return undefined;
    cases.push(...parsed);
  }
  const failed = cases.filter((test) => test.state === "failed");
  // A test that takes less than a tenth of a second is not what makes a suite slow.
  const slow = cases.filter((test) => test.seconds >= SLOW_SECONDS);
  const slowest = slow.sort((a, b) => b.seconds - a.seconds).slice(0, LISTED);
  return {
    tests: cases.length,
    passed: cases.filter((test) => test.state === "passed").length,
    failed: failed.length,
    skipped: cases.filter((test) => test.state === "skipped").length,
    expected_failures: cases.filter((test) => test.expectedFailure).length,
    duration_s: round(
      cases.reduce((total, test) => total + test.seconds, 0),
      3,
    ),
    files: paths.length,
    slowest: slowest.map(({ name, seconds }) => ({ name, seconds: round(seconds, 3) })),
    failed_tests: failed.slice(0, LISTED).map((test) => test.name),
  };
}

// --- Coverage ----------------------------------------------------------------

/** @typedef {{covered: number, total: number}} Ratio */
/** @typedef {{file: string, lines: Ratio, branches: Ratio}} FileCoverage */
/** @typedef {{lines: Ratio, branches: Ratio, files: number, least_covered: FileCoverage[]}} CoverageNumbers */

/** @param {readonly FileCoverage[]} files @returns {CoverageNumbers} */
function coverageNumbers(files) {
  const sum = (/** @type {(file: FileCoverage) => Ratio} */ of) => ({
    covered: files.reduce((total, file) => total + of(file).covered, 0),
    total: files.reduce((total, file) => total + of(file).total, 0),
  });
  const missed = (/** @type {Ratio} */ ratio) => ratio.total - ratio.covered;
  const share = (/** @type {Ratio} */ ratio) => (ratio.total === 0 ? 1 : ratio.covered / ratio.total);
  // The least covered first: by share of lines, then of branches, then by how much is missing.
  const least = files
    .filter((file) => missed(file.lines) > 0 || missed(file.branches) > 0)
    .sort(
      (a, b) =>
        share(a.lines) - share(b.lines) ||
        share(a.branches) - share(b.branches) ||
        missed(b.lines) + missed(b.branches) - missed(a.lines) - missed(a.branches) ||
        a.file.localeCompare(b.file),
    );
  return {
    lines: sum((file) => file.lines),
    branches: sum((file) => file.branches),
    files: files.length,
    least_covered: least.slice(0, LISTED),
  };
}

/**
 * The coverage of a Python project from `coverage json` (coverage.py).
 * @param {string} path @returns {CoverageNumbers | undefined}
 */
export function pythonCoverage(path) {
  const report = object(readJson(path));
  const entries = Object.entries(object(report.files));
  if (report.totals === undefined || entries.length === 0) return undefined;
  /** @type {FileCoverage[]} */
  const files = [];
  for (const [file, value] of entries) {
    const summary = object(object(value).summary);
    const [statements, covered] = [count(summary.num_statements), count(summary.covered_lines)];
    if (statements === undefined || covered === undefined) return undefined;
    files.push({
      file,
      lines: { covered, total: statements },
      branches: { covered: count(summary.covered_branches) ?? 0, total: count(summary.num_branches) ?? 0 },
    });
  }
  return coverageNumbers(files);
}

/**
 * The coverage of a TypeScript project from the Istanbul `coverage-final.json`
 * files vitest wrote, one per suite: a source file measured by several suites
 * is counted once, covered where any of them covered it. A line is one that
 * starts a statement, as in Istanbul's own count.
 * @param {readonly string[]} paths @param {string} root paths are shown relative to it
 * @returns {CoverageNumbers | undefined} `undefined` unless every file is there and readable
 */
export function istanbulCoverage(paths, root) {
  if (paths.length === 0) return undefined;
  /** For each source file, the hits of each line that starts a statement and of each branch arm. */
  /** @type {Map<string, {lines: Map<number, number>, arms: Map<string, number>}>} */
  const sources = new Map();
  for (const path of paths) {
    const report = readJson(path);
    if (typeof report !== "object" || report === null || Array.isArray(report)) return undefined;
    for (const [file, value] of Object.entries(report)) {
      const entry = object(value);
      const [statementMap, statements] = [object(entry.statementMap), object(entry.s)];
      const [branchMap, branches] = [object(entry.branchMap), object(entry.b)];
      const source = sources.get(file) ?? { lines: new Map(), arms: new Map() };
      sources.set(file, source);
      for (const [id, hits] of Object.entries(statements)) {
        const line = count(object(object(statementMap[id]).start).line);
        if (line === undefined) continue;
        source.lines.set(line, Math.max(source.lines.get(line) ?? 0, count(hits) ?? 0));
      }
      for (const [id, arms] of Object.entries(branches)) {
        const branch = object(branchMap[id]);
        const { start, end } = object(branch.loc);
        const at = [object(start).line, object(start).column, object(end).line, object(end).column, branch.type];
        (Array.isArray(arms) ? arms : []).forEach((hits, arm) => {
          const key = `${at.map(String).join(":")}:${arm}`;
          source.arms.set(key, Math.max(source.arms.get(key) ?? 0, count(hits) ?? 0));
        });
      }
    }
  }
  if (sources.size === 0) return undefined;
  const covered = (/** @type {Map<unknown, number>} */ hits) => [...hits.values()].filter((times) => times > 0).length;
  return coverageNumbers(
    [...sources].map(([file, source]) => ({
      file: relative(root, file).split("\\").join("/"),
      lines: { covered: covered(source.lines), total: source.lines.size },
      branches: { covered: covered(source.arms), total: source.arms.size },
    })),
  );
}

/**
 * The `coverage-final.json` of each suite, all of them or none: a total made
 * of some of the suites would read lower than what was measured.
 * @param {string} dir @param {readonly string[]} suites @returns {string[]}
 */
function coverageFiles(dir, suites) {
  const files = suites.map((suite) => join(dir, `coverage-${suite}`, "coverage-final.json"));
  return files.every((file) => existsSync(file)) ? files : [];
}

// --- Lint and types ------------------------------------------------------------

/**
 * @typedef {{kind: "lint" | "types", project: string, job: string, name: string,
 *   state: "passed" | "failed" | "not_run"}} Check
 */

/** A stage of a `check.sh` that is a lint or a type check: `lint (ruff)`, `gate helpers: types (mypy)`. */
const CHECK_STAGE = /^(gate helpers: )?(lint|format|types) \(.+\)$/;

/**
 * The lint and type checks among the stages a `check.sh` recorded (one line
 * per stage: `passed` or `failed`, a tab, the name of the stage).
 * @param {string | undefined} text the content of `stages.tsv` @param {string} project whose gate it was
 * @param {string} job the gate @returns {Check[]}
 */
export function stageChecks(text, project, job) {
  /** @type {Check[]} */
  const checks = [];
  for (const line of (text ?? "").split("\n")) {
    const [state, name = ""] = line.split("\t");
    const stage = CHECK_STAGE.exec(name);
    if (stage === null || (state !== "passed" && state !== "failed")) continue;
    const kind = stage[2] === "types" ? "types" : "lint";
    // The helpers of the gates live in scripts/ci: they count for the scripts.
    checks.push({ kind, project: stage[1] ? "scripts" : project, job, name, state });
  }
  return checks;
}

/**
 * A check that is a step of a job, from the outcome GitHub gives for the step.
 * @param {"lint" | "types"} kind @param {readonly string[]} projects @param {string} job @param {string} name
 * @param {string | undefined} outcome @returns {Check[]}
 */
const stepChecks = (kind, projects, job, name, outcome) =>
  projects.map((project) => ({
    kind,
    project,
    job,
    name,
    state: outcome === "success" ? "passed" : outcome === "failure" ? "failed" : "not_run",
  }));

/**
 * The same check run by several jobs (the simulation lints in each job of its
 * battery) counts once: failed if it failed anywhere.
 * @param {readonly Check[]} checks @returns {Check[]}
 */
function once(checks) {
  /** @type {Map<string, Check>} */
  const kept = new Map();
  for (const check of checks) {
    const key = `${check.kind}\t${check.project}\t${check.name}`;
    const known = kept.get(key);
    if (known === undefined || (check.state === "failed" && known.state !== "failed")) kept.set(key, { ...check });
  }
  return [...kept.values()];
}

// --- The numbers of one job ----------------------------------------------------

/** @typedef {{runs: number, by_status: Record<string, number>, by_group: Record<string, number>}} Scenarios */
/**
 * @typedef {{schema: number, part: string, suites: Record<string, SuiteNumbers>,
 *   coverage: Record<string, CoverageNumbers>, checks: Check[], scenarios?: Scenarios}} Part
 */

/**
 * The runs of the synthetic simulation report (`simulation.quick --all`, `report.json`).
 * @param {string | undefined} path @returns {Scenarios | undefined}
 */
export function scenarios(path) {
  if (path === undefined) return undefined;
  const report = object(readJson(path));
  if (!Array.isArray(report.runs)) return undefined;
  /** @type {Record<string, number>} */
  const byStatus = {};
  /** @type {Record<string, number>} */
  const byGroup = {};
  for (const run of report.runs) {
    const status = String(object(run).status ?? "?");
    const group = String(object(run).group ?? "?");
    byStatus[status] = (byStatus[status] ?? 0) + 1;
    byGroup[group] = (byGroup[group] ?? 0) + 1;
  }
  return { runs: report.runs.length, by_status: byStatus, by_group: byGroup };
}

/**
 * What one job measured, from the files its tools left.
 * @param {string} part `pi`, `simulation`, `convex` or `site`
 * @param {{dir: string, parts?: string, scenarios?: string, shares?: number, root: string,
 *   env: Readonly<Record<string, string | undefined>>}} from `shares`: how many processes ran the Python suite
 * @returns {Part}
 */
export function buildPart(part, { dir, parts, scenarios: scenariosFile, shares: expected, root, env }) {
  /** @type {Record<string, SuiteNumbers | undefined>} */
  let suites = {};
  /** @type {Record<string, CoverageNumbers | undefined>} */
  let coverage = {};
  /** @type {Check[]} */
  let checks = [];
  const junit = (/** @type {string} */ name) => suiteNumbers(existsSync(join(dir, name)) ? [join(dir, name)] : []);
  const shares = (/** @type {string} */ directory) =>
    namesIn(directory)
      .filter((name) => /^junit-\d+\.xml$/.test(name))
      .map((name) => join(directory, name));
  if (part === "pi") {
    suites = {
      "pi-pytest": suiteNumbers(shares(dir), expected),
      "scripts-gate-runner": junit("junit-gate-runner.xml"),
    };
    coverage = {
      pi: pythonCoverage(join(dir, "coverage-all.json")),
      "pi-safety-chain": pythonCoverage(join(dir, "coverage-gate.json")),
    };
    checks = stageChecks(readText(join(dir, "stages.tsv")), "pi", "pi-gate");
  } else if (part === "simulation") {
    // The jobs of the battery each left a directory: their stages and one JUnit file per share.
    const jobs = namesIn(parts ?? "")
      .filter((name) => name.startsWith("quality-"))
      .map((name) => join(parts ?? "", name));
    suites = { "simulation-battery": suiteNumbers(jobs.flatMap(shares), expected) };
    coverage = { simulation: pythonCoverage(join(dir, "coverage-gate.json")) };
    checks = once(
      jobs.flatMap((job) => stageChecks(readText(join(job, "stages.tsv")), "simulation", "simulation-gate")),
    );
  } else if (part === "convex") {
    suites = { convex: junit("convex.xml") };
    coverage = { convex: istanbulCoverage(coverageFiles(dir, ["convex"]), root) };
    checks = stepChecks("types", ["convex"], "convex-tests", "tsc -p convex/tsconfig.json", env.TYPES_OUTCOME);
  } else if (part === "site") {
    suites = {
      "site-lib": junit("site-lib.xml"),
      "site-components": junit("site-components.xml"),
      "pi-panel": junit("pi-panel.xml"),
    };
    coverage = { site: istanbulCoverage(coverageFiles(dir, ["site-lib", "site-components"]), root) };
    checks = [
      ...stepChecks("types", ["site"], "web", "tsc --noEmit", env.TYPES_OUTCOME),
      // One ESLint run reads the whole repository.
      ...stepChecks("lint", ["site", "convex", "scripts"], "web", "eslint .", env.LINT_OUTCOME),
    ];
  } else {
    throw new Error(`unknown part "${part}"`);
  }
  /** @template T @param {Record<string, T | undefined>} entries @returns {Record<string, T>} */
  const measured = (entries) =>
    Object.fromEntries(Object.entries(entries).flatMap(([id, value]) => (value === undefined ? [] : [[id, value]])));
  /** @type {Part} */
  const result = { schema: SCHEMA_VERSION, part, suites: measured(suites), coverage: measured(coverage), checks };
  const runs = part === "simulation" ? scenarios(scenariosFile) : undefined;
  if (runs !== undefined) result.scenarios = runs;
  return result;
}

// --- Writing Markdown ------------------------------------------------------------

/** @param {string} text any text @returns {string} the same text, safe in a cell of a Markdown table */
export const cell = (text) =>
  `<code>${text
    .slice(0, 160)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll("|", "&#124;")
    .replaceAll(/\s+/g, " ")}</code>`;

/** @param {number} value @returns {string} a whole number, its thousands set apart by a space that does not break */
const whole = (value) => String(value).replace(/\B(?=(\d{3})+$)/g, " ");

/** @param {number} value @param {string} noun in the singular @returns {string} a count with its noun, in French */
const counted = (value, noun) => `${whole(value)} ${noun}${value > 1 ? "s" : ""}`;

/**
 * A share as a percentage, never rounded up: 99,9 % stays below 100 %.
 * @param {Ratio | undefined} ratio @returns {string}
 */
export function percent(ratio) {
  if (ratio === undefined) return "indisponible";
  if (ratio.total === 0) return "sans objet";
  if (ratio.covered >= ratio.total) return "100 %";
  return `${(Math.floor((ratio.covered * 1000) / ratio.total) / 10).toFixed(1).replace(".", ",")} %`;
}

/** @param {number} seconds @returns {string} */
export function duration(seconds) {
  if (seconds < 60) return `${seconds.toFixed(1).replace(".", ",")} s`;
  const total = Math.round(seconds);
  const [hours, minutes, rest] = [Math.floor(total / 3600), Math.floor((total % 3600) / 60), total % 60];
  if (hours > 0) return `${hours} h ${String(minutes).padStart(2, "0")} min`;
  return `${minutes} min ${String(rest).padStart(2, "0")} s`;
}

/** @param {readonly string[]} header @param {readonly (readonly string[])[]} rows @param {string} [align] */
const table = (header, rows, align = "") =>
  [
    `| ${header.join(" | ")} |`,
    `|${header.map((_, index) => (align[index] === "r" ? "---:" : "---")).join("|")}|`,
    ...rows.map((row) => `| ${row.join(" | ")} |`),
  ].join("\n");

/** @param {number} times @param {string} word @returns {string[]} the same word in several cells */
const cells = (times, word) => Array.from({ length: times }, () => word);

/** @param {Ratio} ratio */
const ratioText = (ratio) => `${percent(ratio)} (${whole(ratio.covered)} sur ${whole(ratio.total)})`;

/** @type {Readonly<Record<string, string>>} */
const CHECK_WORD = { passed: "réussi", failed: "échec", not_run: "non lancé" };
/** @type {Readonly<Record<string, string>>} */
const PART_TITLE = { pi: "console du Pi", simulation: "simulation", convex: "Convex", site: "site" };

/** @param {Scenarios} runs @returns {string} */
function scenariosText(runs) {
  const statuses = Object.entries(runs.by_status)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([status, times]) => `${whole(times)} ${status}`);
  return `${whole(runs.runs)} scénarios joués par \`simulation.quick --all\` : ${statuses.join(", ")}`;
}

/**
 * The summary a gate job shows for itself: its suites, its coverage with the
 * least covered files, its slowest tests. Folded, so that the table of the
 * run stays the first thing read on the page.
 * @param {Part} part @returns {string}
 */
export function renderPart(part) {
  const lines = [`<details><summary><b>Détail de la qualité : ${PART_TITLE[part.part] ?? part.part}</b></summary>`, ""];
  const suites = SUITES.filter((suite) => suite.part === part.part);
  lines.push(
    "#### Tests",
    "",
    table(
      ["Suite", "Outil", "Lancés", "Réussis", "Échoués", "Ignorés", "Durée cumulée"],
      suites.map((suite) => {
        const numbers = part.suites[suite.id];
        if (numbers === undefined) return [suite.label, suite.runner, ...cells(5, "indisponible")];
        const expected = numbers.expected_failures > 0 ? ` (dont ${numbers.expected_failures} xfail)` : "";
        return [
          suite.label,
          suite.runner,
          whole(numbers.tests),
          whole(numbers.passed),
          whole(numbers.failed),
          `${whole(numbers.skipped)}${expected}`,
          duration(numbers.duration_s),
        ];
      }),
      "llrrrrr",
    ),
    "",
  );
  const failed = suites.flatMap((suite) => part.suites[suite.id]?.failed_tests ?? []);
  if (failed.length > 0) lines.push("Tests en échec :", "", ...failed.map((name) => `- ${cell(name)}`), "");
  const measures = COVERAGES.filter((measure) => measure.part === part.part);
  lines.push(
    "#### Couverture",
    "",
    table(
      ["Mesure", "Fichiers", "Lignes", "Branches", "Seuil exigé"],
      measures.map((measure) => {
        const numbers = part.coverage[measure.id];
        const required = measure.threshold === undefined ? "aucun" : `${measure.threshold} %`;
        if (numbers === undefined) return [measure.label, ...cells(3, "indisponible"), required];
        return [measure.label, whole(numbers.files), ratioText(numbers.lines), ratioText(numbers.branches), required];
      }),
      "lrrrl",
    ),
    "",
  );
  for (const measure of measures) {
    const least = part.coverage[measure.id]?.least_covered ?? [];
    if (least.length === 0) continue;
    lines.push(
      `Fichiers les moins couverts (${measure.label}) :`,
      "",
      table(
        ["Fichier", "Lignes", "Branches"],
        least.map((file) => [cell(file.file), ratioText(file.lines), ratioText(file.branches)]),
        "lrr",
      ),
      "",
    );
  }
  if (part.scenarios !== undefined) lines.push("#### Scénarios", "", `${scenariosText(part.scenarios)}.`, "");
  for (const suite of suites) {
    const slowest = part.suites[suite.id]?.slowest ?? [];
    if (slowest.length === 0) continue;
    lines.push(
      `Tests les plus lents (${suite.label}) :`,
      "",
      table(
        ["Test", "Durée"],
        slowest.map((test) => [cell(test.name), duration(test.seconds)]),
        "lr",
      ),
      "",
    );
  }
  if (part.checks.length > 0) {
    lines.push(
      "#### Lint et types",
      "",
      table(
        ["Contrôle", "Projet", "État"],
        part.checks.map((check) => [cell(check.name), check.project, CHECK_WORD[check.state] ?? check.state]),
      ),
      "",
    );
  }
  lines.push("</details>", "");
  return lines.join("\n");
}

// --- The report of the run -------------------------------------------------------

/** @typedef {"passed" | "failed" | "cancelled" | "skipped" | "not_run" | "unknown"} JobState */
/** @typedef {"skipped" | "not_run" | "unavailable"} Absence why something that is expected is not there */

/**
 * How each job the report waits for ended. A job GitHub reports as skipped
 * was skipped by the path rule only if the rule ran, on a pull request, and
 * answered "false" for it: otherwise it simply did not run.
 * @param {unknown} needs the `needs` context of the report job @param {string} event the name of the event
 * @returns {Record<string, JobState>}
 */
export function jobStates(needs, event) {
  const all = object(needs);
  const changes = object(all.changes);
  const answers = changes.result === "success" && event === "pull_request" ? object(changes.outputs) : {};
  /** @type {Record<string, JobState>} */
  const states = {};
  for (const job of ["changes", ...GATES]) {
    const result = object(all[job]).result;
    const rule = RULE[job];
    if (result === "success") states[job] = "passed";
    else if (result === "failure") states[job] = "failed";
    else if (result === "cancelled") states[job] = "cancelled";
    else if (result === "skipped")
      states[job] = rule !== undefined && answers[rule] === "false" ? "skipped" : "not_run";
    else states[job] = "unknown";
  }
  return states;
}

/** From the worst to the best, for a line that depends on several jobs. */
const WORST = /** @type {const} */ (["failed", "cancelled", "unknown", "not_run", "skipped", "passed"]);
/** @param {readonly JobState[]} states @returns {JobState} */
const worst = (states) => WORST.find((state) => states.includes(state)) ?? "unknown";

/**
 * The lint, or the types, of one project: failed if a check failed, passed if
 * at least one ran and none failed. `complete` is false when one of the jobs
 * these checks come from left none that ran.
 * @param {readonly Check[]} checks @param {string} project @param {"lint" | "types"} kind
 * @param {(job: string) => Absence} absent why a job left nothing
 */
function checkState(checks, project, kind, absent) {
  const own = checks.filter((check) => check.project === project && check.kind === kind);
  const ran = own.filter((check) => check.state !== "not_run");
  const silent = (CHECK_JOBS[project]?.[kind] ?? []).filter((job) => !ran.some((check) => check.job === job));
  /** @type {"passed" | "failed" | Absence} */
  let state = "unavailable";
  if (ran.some((check) => check.state === "failed")) state = "failed";
  else if (ran.length > 0) state = "passed";
  else if (own.length > 0) state = "not_run";
  else if (silent.length > 0) state = silent.map(absent).find((why) => why !== "skipped") ?? "skipped";
  return {
    state,
    complete: silent.length === 0,
    checks: own.map(({ job, name, state: ended }) => ({ job, name, state: ended })),
  };
}

/**
 * The report of a run: every project, from the numbers each job left and from
 * how each job ended. Nothing in it is assumed: a number that is missing is
 * named as missing, with the reason.
 * @param {{parts: Readonly<Record<string, Part | undefined>>, junit: Readonly<Record<string, SuiteNumbers | undefined>>,
 *   needs: unknown, event: string, run: Record<string, unknown>}} input `junit`: the suites whose job left its
 *   JUnit file as it is, by suite
 */
export function buildReport({ parts, junit, needs, event, run }) {
  const jobs = jobStates(needs, event);
  /** @param {string} job @returns {Absence} why a job left no numbers */
  const absent = (job) => {
    if (jobs[job] === "skipped") return "skipped";
    return jobs[job] === "passed" || jobs[job] === "failed" ? "unavailable" : "not_run";
  };
  const suites = SUITES.map(({ id, project, job, part, label, runner }) => {
    const numbers = parts[part]?.suites[id] ?? junit[id];
    /** @type {"measured" | Absence} */
    const state = numbers === undefined ? absent(job) : "measured";
    return { id, project, job, label, runner, state, ...(numbers === undefined ? {} : { numbers }) };
  });
  const coverage = COVERAGES.map(({ id, project, job, part, label, main, threshold }) => {
    const numbers = parts[part]?.coverage[id];
    /** @type {"measured" | Absence} */
    const state = numbers === undefined ? absent(job) : "measured";
    return {
      id,
      project,
      job,
      label,
      main,
      ...(threshold === undefined ? {} : { threshold }),
      state,
      ...(numbers === undefined ? {} : { numbers }),
    };
  });
  const checks = Object.values(parts).flatMap((part) => part?.checks ?? []);
  const projects = PROJECTS.map(({ id, label, gates }) => {
    const own = suites.filter((suite) => suite.project === id);
    const measured = own.flatMap((suite) => (suite.numbers === undefined ? [] : [suite.numbers]));
    const total = (/** @type {(numbers: SuiteNumbers) => number} */ of) =>
      measured.reduce((sum, numbers) => sum + of(numbers), 0);
    const main = coverage.find((measure) => measure.project === id && measure.main);
    return {
      id,
      label,
      gate: {
        state: worst(gates.map((job) => jobs[job] ?? "unknown")),
        jobs: gates.map((job) => ({ job, state: jobs[job] ?? "unknown" })),
      },
      tests:
        measured.length === 0
          ? null
          : {
              total: total((numbers) => numbers.tests),
              passed: total((numbers) => numbers.passed),
              failed: total((numbers) => numbers.failed),
              skipped: total((numbers) => numbers.skipped),
              duration_s: round(
                total((numbers) => numbers.duration_s),
                3,
              ),
              // False when a suite of this project left no numbers: the totals then cover part of it.
              complete: measured.length === own.length,
            },
      tests_state:
        measured.length > 0
          ? "measured"
          : (own.map((suite) => suite.state).find((state) => state !== "measured") ?? "unavailable"),
      coverage: main?.numbers === undefined ? null : { lines: main.numbers.lines, branches: main.numbers.branches },
      coverage_state: main === undefined ? "not_measured" : main.state,
      lint: checkState(checks, id, "lint", absent),
      types: checkState(checks, id, "types", absent),
    };
  });
  return {
    schema: SCHEMA_VERSION,
    run,
    jobs,
    projects,
    suites,
    coverage,
    scenarios: parts.simulation?.scenarios ?? null,
  };
}

/** @typedef {ReturnType<typeof buildReport>} Report */

/** @type {Readonly<Record<string, string>>} */
const GATE_WORD = {
  passed: "✅ réussie",
  failed: "❌ échec",
  cancelled: "⚪ annulée",
  skipped: "⏭️ sautée",
  not_run: "⚪ non lancée",
  unknown: "❔ état inconnu",
};
/** @type {Readonly<Record<string, string>>} */
const MISSING = { skipped: "sautée", not_run: "non lancé", unavailable: "indisponible", not_measured: "non mesurée" };

/** @param {Report["projects"][number]["gate"]} gate @returns {string} the verdict, and the jobs it is the verdict of */
function gateText(gate) {
  const decisive = gate.jobs.filter(({ state }) => state === gate.state);
  return `${GATE_WORD[gate.state] ?? gate.state} : ${decisive.map(({ job }) => `\`${job}\``).join(", ")}`;
}

/** @param {{state: string, complete: boolean}} check @returns {string} */
function checkText(check) {
  if (check.state === "passed") return `✅ réussi${check.complete ? "" : " (partiel)"}`;
  if (check.state === "failed") return "❌ échec";
  return MISSING[check.state] ?? check.state;
}

/** @param {Report} report @returns {string[][]} the safety chain and the simulation, one measure per line */
function safetyRows(report) {
  /** @type {string[][]} */
  const rows = [];
  for (const measure of report.coverage.filter(({ threshold }) => threshold !== undefined)) {
    const required = `${measure.threshold} % exigé par \`${measure.job}\``;
    if (measure.numbers === undefined) {
      rows.push([`Couverture, ${measure.label}`, MISSING[measure.state] ?? measure.state, required]);
      continue;
    }
    const { lines, branches } = measure.numbers;
    const held = lines.covered >= lines.total && branches.covered >= branches.total;
    rows.push([
      `Couverture, ${measure.label}`,
      `lignes ${ratioText(lines)}, branches ${ratioText(branches)}`,
      `${required} : ${held ? "✅ tenu" : "❌ non tenu"}`,
    ]);
  }
  const battery = report.suites.find((suite) => suite.id === "simulation-battery");
  /** What to say of the simulation where it left nothing. */
  const without = MISSING[battery === undefined || battery.state === "measured" ? "unavailable" : battery.state] ?? "";
  const numbers = battery?.numbers;
  rows.push([
    "Batterie de simulation",
    numbers === undefined
      ? without
      : `${counted(numbers.tests, "test")} : ${counted(numbers.passed, "réussi")}, ${whole(numbers.failed)} en échec, ` +
        `${counted(numbers.skipped, "ignoré")} (dont ${whole(numbers.expected_failures)} xfail)`,
    "aucun échec, chaque test une seule fois (`simulation-gate`)",
  ]);
  rows.push([
    "Scénarios de simulation",
    report.scenarios === null ? without : scenariosText(report.scenarios),
    "aucun FAIL (`simulation (report)`)",
  ]);
  return rows;
}

/** @param {Record<string, unknown>} run @returns {string} */
function runText(run) {
  const words = [];
  if (typeof run.pull_request === "number") words.push(`PR #${run.pull_request}`);
  if (typeof run.sha === "string" && run.sha !== "") words.push(`commit \`${run.sha.slice(0, 7)}\``);
  if (typeof run.event === "string") words.push(`événement \`${run.event}\``);
  if (run.id !== null && run.id !== undefined)
    words.push(`exécution ${String(run.id)}, tentative ${String(run.attempt)}`);
  return words.join(" · ");
}

/** @param {Report} report @returns {string} the summary of the run, in Markdown */
export function renderReport(report) {
  const { run, jobs, projects, suites } = report;
  const measured = suites.flatMap((suite) => (suite.numbers === undefined ? [] : [suite.numbers]));
  const tests = measured.reduce((sum, numbers) => sum + numbers.tests, 0);
  const failed = measured.reduce((sum, numbers) => sum + numbers.failed, 0);
  const told = (/** @type {JobState} */ state, /** @type {string} */ words) => {
    const named = GATES.filter((job) => jobs[job] === state).map((job) => `\`${job}\``);
    return named.length === 0 ? [] : [`${named.length} ${words} (${named.join(", ")})`];
  };
  const verdict = [
    `${GATES.filter((job) => jobs[job] === "passed").length} gates réussies sur ${GATES.length}`,
    ...told("failed", "en échec"),
    ...told("skipped", "sautées par la règle de chemins"),
    ...told("cancelled", "annulées"),
    ...told("not_run", "non lancées"),
    ...told("unknown", "dans un état inconnu"),
  ];
  const lines = [
    "## Rapport de qualité",
    "",
    `**${whole(tests)} tests lancés, ${whole(failed)} en échec.** ${verdict.join(", ")}.`,
    "",
    table(
      [
        "Projet",
        "Tests lancés",
        "Réussis",
        "Échoués",
        "Ignorés",
        "Durée cumulée",
        "Lignes couvertes",
        "Branches couvertes",
        "Lint",
        "Types",
        "Gate",
      ],
      projects.map((project) => {
        const gate = gateText(project.gate);
        if (project.gate.state === "skipped") return [project.label, ...cells(9, MISSING.skipped ?? ""), gate];
        const numbers = project.tests;
        const counted =
          numbers === null
            ? cells(5, MISSING[project.tests_state] ?? project.tests_state)
            : [
                `${whole(numbers.total)}${numbers.complete ? "" : " ¹"}`,
                whole(numbers.passed),
                numbers.failed > 0 ? `**${whole(numbers.failed)}**` : "0",
                whole(numbers.skipped),
                duration(numbers.duration_s),
              ];
        const covered =
          project.coverage === null
            ? cells(2, MISSING[project.coverage_state] ?? project.coverage_state)
            : [percent(project.coverage.lines), percent(project.coverage.branches)];
        return [project.label, ...counted, ...covered, checkText(project.lint), checkText(project.types), gate];
      }),
      "lrrrrrrrlll",
    ),
    "",
  ];
  if (projects.some((project) => project.gate.state !== "skipped" && project.tests?.complete === false)) {
    lines.push("¹ Une suite de ce projet n'a pas de chiffres dans cette exécution : voir le détail par suite.", "");
  }
  const others = ["audit", "docs"].map((job) => `\`${job}\` ${GATE_WORD[jobs[job] ?? "unknown"] ?? ""}`);
  lines.push(
    `Autres gates : ${others.join(" · ")}. Ce qu'elles contrôlent se lit dans leur journal, pas ici.`,
    "",
    "### Chaîne de sécurité et simulation",
    "",
    table(["Mesure", "Valeur", "Exigence"], safetyRows(report)),
    "",
    "### Détail par suite de tests",
    "",
    table(
      ["Projet", "Suite", "Outil", "Job", "Lancés", "Réussis", "Échoués", "Ignorés", "Durée cumulée"],
      suites.map((suite) => {
        const project = PROJECTS.find(({ id }) => id === suite.project)?.label.replace(/ \(.*$/, "") ?? suite.project;
        const head = [project, suite.label, suite.runner, `\`${suite.job}\``];
        if (suite.numbers === undefined) return [...head, ...cells(5, MISSING[suite.state] ?? suite.state)];
        const { tests: ran, passed, failed: broken, skipped, duration_s: seconds } = suite.numbers;
        return [...head, whole(ran), whole(passed), whole(broken), whole(skipped), duration(seconds)];
      }),
      "llllrrrrr",
    ),
    "",
    "### Lire ce rapport",
    "",
    "- « sautée » : la règle de chemins a jugé qu'aucun fichier de la PR ne concerne cette gate, qui n'a pas tourné.",
    "- « indisponible » : le job a tourné sans laisser ces chiffres. « non lancé » : le job n'a pas tourné.",
    "- Durée cumulée : la somme des durées de chaque test, tous processus confondus. Ce n'est pas l'attente du job.",
    "- Couverture de Convex et du site : mesurée, sans seuil. Elle ne fait échouer aucune gate.",
    "- Non mesuré : la couverture des scripts ; les pages du site (`app/`), qu'aucun test unitaire ne charge ; " +
      "les tests de release (`npm run test:release`), que la CI ne lance pas.",
    "- Chaque colonne est décrite dans `docs/framework-de-test.md`, section CI. Les mêmes chiffres sont dans " +
      "l'artefact `quality-report` (`quality-report.json`).",
    "",
    `<sub>${runText(run)}</sub>`,
    "",
  );
  return lines.join("\n");
}

/** @type {Readonly<Record<string, string>>} */
const GATE_SAID = {
  passed: "réussie",
  failed: "en échec",
  cancelled: "annulée",
  skipped: "sautée",
  not_run: "non lancée",
  unknown: "dans un état inconnu",
};
/** @type {Readonly<Record<string, string>>} */
const COVERAGE_SAID = {
  skipped: "sautée",
  not_run: "non lancée",
  unavailable: "indisponible",
  not_measured: "non mesurée",
};
/** A state in a sentence, after "lint" (the first word) and after "tests" or "types" (the second). */
/** @type {Readonly<Record<string, readonly [string, string]>>} */
const SAID = {
  passed: ["réussi", "réussis"],
  failed: ["en échec", "en échec"],
  skipped: ["sauté", "sautés"],
  not_run: ["non lancé", "non lancés"],
  unavailable: ["indisponible", "indisponibles"],
};

/**
 * The verdict of the run and one line per project, as plain text: what the
 * report job also says as a notice. GitHub shows the summaries of the jobs in
 * the order the jobs ended, so the table of the run, written by the last job,
 * is the last block of the page; a notice is listed with the annotations of
 * the run. Counts and states only, like the table.
 * @param {Report} report @returns {string}
 */
export function renderNotice(report) {
  const { jobs, projects, suites } = report;
  const measured = suites.flatMap((suite) => (suite.numbers === undefined ? [] : [suite.numbers]));
  const sum = (/** @type {(numbers: SuiteNumbers) => number} */ of) =>
    measured.reduce((total, numbers) => total + of(numbers), 0);
  const passed = GATES.filter((job) => jobs[job] === "passed").length;
  const others = GATES.filter((job) => jobs[job] !== "passed").map(
    (job) => `${job} ${GATE_SAID[jobs[job] ?? "unknown"]}`,
  );
  const lines = [
    `${whole(sum((numbers) => numbers.tests))} tests lancés, ${whole(sum((numbers) => numbers.failed))} en échec. ` +
      `${passed} gates réussies sur ${GATES.length}${others.length === 0 ? "" : ` (${others.join(", ")})`}.`,
  ];
  for (const project of projects) {
    const label = project.label.replace(/ \(.*$/, "");
    if (project.gate.state === "skipped") {
      lines.push(`${label} : sautée par la règle de chemins`);
      continue;
    }
    const partly = (/** @type {{state: string, complete: boolean}} */ { state, complete }) =>
      state === "passed" && !complete ? " (partiel)" : "";
    lines.push(
      `${label} : ` +
        [
          project.tests === null
            ? `tests ${SAID[project.tests_state]?.[1] ?? project.tests_state}`
            : `${whole(project.tests.total)} tests, ${whole(project.tests.failed)} en échec`,
          project.coverage === null
            ? `couverture ${COVERAGE_SAID[project.coverage_state] ?? project.coverage_state}`
            : `lignes ${percent(project.coverage.lines)}, branches ${percent(project.coverage.branches)}`,
          `lint ${SAID[project.lint.state]?.[0] ?? project.lint.state}${partly(project.lint)}`,
          `types ${SAID[project.types.state]?.[1] ?? project.types.state}${partly(project.types)}`,
          `gate ${GATE_SAID[project.gate.state] ?? project.gate.state}`,
        ].join(" ; "),
    );
  }
  lines.push("Le tableau complet est le résumé du job quality-report, dernier bloc de cette page.");
  return lines.join("\n");
}

/**
 * A notice, as the runner reads it from the output of a step.
 * @param {string} title @param {string} message @returns {string}
 */
export function notice(title, message) {
  const data = (/** @type {string} */ text) =>
    text.replaceAll("%", "%25").replaceAll("\r", "%0D").replaceAll("\n", "%0A");
  return `::notice title=${data(title).replaceAll(":", "%3A").replaceAll(",", "%2C")}::${data(message)}`;
}

// --- Commands --------------------------------------------------------------------

/** @param {readonly string[]} argv @returns {Record<string, string | undefined>} `--name value` pairs */
function options(argv) {
  /** @type {Record<string, string | undefined>} */
  const found = {};
  for (let index = 0; index < argv.length; index += 2) {
    const [name = "", value] = [argv[index], argv[index + 1]];
    if (!name.startsWith("--") || value === undefined) throw new Error(`unexpected argument "${name}"`);
    found[name.slice(2)] = value;
  }
  return found;
}

/** @param {Readonly<Record<string, string | undefined>>} env @param {string} markdown */
function summarize(env, markdown) {
  if (env.GITHUB_STEP_SUMMARY) appendFileSync(env.GITHUB_STEP_SUMMARY, `${markdown}\n`);
  else process.stdout.write(`${markdown}\n`);
}

/** @param {Readonly<Record<string, string | undefined>>} env @returns {Record<string, unknown>} what names the run */
function runOf(env) {
  const pull = Number(env.PR_NUMBER);
  const known = env.GITHUB_SERVER_URL && env.GITHUB_REPOSITORY && env.GITHUB_RUN_ID;
  return {
    id: env.GITHUB_RUN_ID ?? null,
    attempt: Number(env.GITHUB_RUN_ATTEMPT) || 1,
    event: env.GITHUB_EVENT_NAME ?? null,
    repository: env.GITHUB_REPOSITORY ?? null,
    ref: env.GITHUB_REF_NAME ?? null,
    sha: env.HEAD_SHA || env.GITHUB_SHA || null,
    pull_request: Number.isInteger(pull) && pull > 0 ? pull : null,
    url: known ? `${env.GITHUB_SERVER_URL}/${env.GITHUB_REPOSITORY}/actions/runs/${env.GITHUB_RUN_ID}` : null,
  };
}

/** @param {readonly string[]} given @param {Readonly<Record<string, string | undefined>>} env */
function job(given, env) {
  const [part = "", ...rest] = given;
  const { dir, parts, scenarios: scenariosFile, shares } = options(rest);
  if (!dir) throw new Error("job: --dir is required");
  const expected = shares === undefined ? undefined : Number(shares);
  if (expected !== undefined && !(Number.isInteger(expected) && expected > 0))
    throw new Error("job: --shares is a count");
  const root = resolve(env.GITHUB_WORKSPACE ?? ".");
  mkdirSync(dir, { recursive: true });
  const numbers = buildPart(part, { dir, parts, scenarios: scenariosFile, shares: expected, root, env });
  writeFileSync(join(dir, "part.json"), `${JSON.stringify(numbers, null, 2)}\n`);
  summarize(env, renderPart(numbers));
}

/** @param {readonly string[]} given @param {Readonly<Record<string, string | undefined>>} env */
function report(given, env) {
  const { artifacts, out } = options(given);
  if (!artifacts || !out) throw new Error("report: --artifacts and --out are required");
  /** @type {Record<string, Part | undefined>} */
  const parts = {};
  /** @type {Record<string, SuiteNumbers | undefined>} */
  const junit = {};
  for (const part of new Set([...SUITES, ...COVERAGES].map((entry) => entry.part))) {
    const read = object(readJson(join(artifacts, `quality-${part}`, "part.json")));
    if (read.schema === SCHEMA_VERSION && read.part === part)
      parts[part] = /** @type {Part} */ (/** @type {unknown} */ (read));
  }
  // A job that only runs a test file of the scripts leaves its JUnit file as it is.
  for (const suite of SUITES) {
    const file = join(artifacts, `quality-${suite.part}`, `${suite.id}.xml`);
    if (existsSync(file)) junit[suite.id] = suiteNumbers([file]);
  }
  /** @type {unknown} */
  let needs = {};
  try {
    needs = JSON.parse(env.NEEDS ?? "");
  } catch {
    // Without it every job is in an unknown state, and the report says so.
  }
  const built = buildReport({ parts, junit, needs, event: env.GITHUB_EVENT_NAME ?? "", run: runOf(env) });
  const markdown = renderReport(built);
  mkdirSync(out, { recursive: true });
  writeFileSync(join(out, "quality-report.json"), `${JSON.stringify(built, null, 2)}\n`);
  writeFileSync(join(out, "quality-report.md"), `${markdown}\n`);
  summarize(env, markdown);
  // On GitHub only: the essentials again, where the page of the run lists its annotations.
  if (env.GITHUB_ACTIONS === "true") process.stdout.write(`${notice("Rapport de qualité", renderNotice(built))}\n`);
}

/**
 * The command line. It never fails the job that calls it: a report that
 * cannot be written is said in the summary, and the exit code stays 0.
 * @param {readonly string[]} argv @param {Readonly<Record<string, string | undefined>>} env @returns {number}
 */
export function main(argv, env) {
  const [command, ...given] = argv;
  try {
    if (command === "job") job(given, env);
    else if (command === "report") report(given, env);
    else throw new Error(`unknown command "${command ?? ""}": job or report`);
  } catch (error) {
    console.error(`quality report: ${error instanceof Error ? error.message : String(error)}`);
    const what = command === "job" ? `Détail de la qualité (${given[0] ?? "?"})` : "Rapport de qualité";
    try {
      summarize(
        env,
        `**${what} : rapport indisponible.** L'outil du rapport a échoué ; aucune gate ne dépend de lui.\n`,
      );
    } catch {
      // Nothing more can be said, and nothing may fail here.
    }
  }
  return 0;
}

if (process.argv[1] !== undefined && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  process.exitCode = main(process.argv.slice(2), process.env);
}
