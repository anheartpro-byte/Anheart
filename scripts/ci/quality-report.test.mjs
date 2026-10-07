// What must stay true of the quality report of a CI run (ANH-199,
// docs/framework-de-test.md, section CI, "Rapport de qualité").
//
// Run by the `changes` job of ci.yml with the other CI test files, without any
// install. The inputs are written here as the tools write them: JUnit files of
// pytest, of vitest and of `node --test`, `coverage json` of coverage.py,
// `coverage-final.json` of vitest, the stages a gate script records. Each run
// of the report is one a real run can produce: every gate green, a gate the
// path rule skipped, a gate that failed, a job that left nothing.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  buildPart,
  buildReport,
  cell,
  CHECK_JOBS,
  COVERAGES,
  duration,
  GATES,
  istanbulCoverage,
  jobStates,
  main,
  notice,
  parseJunit,
  percent,
  PROJECTS,
  pythonCoverage,
  renderNotice,
  renderPart,
  renderReport,
  SCHEMA_VERSION,
  stageChecks,
  suiteNumbers,
  SUITES,
} from "./quality-report.mjs";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

/** A directory of its own for one test, removed when the test ends. @param {import("node:test").TestContext} context */
function scratch(context) {
  const directory = mkdtempSync(join(tmpdir(), "anheart-quality-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  return directory;
}

/** @param {string} directory @param {string} name @param {string} content @returns {string} the path written */
function write(directory, name, content) {
  const path = join(directory, name);
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, content);
  return path;
}

/**
 * A summary as it reads: thousands are set apart by a space that does not
 * break, written here as a plain space.
 * @param {string} markdown
 */
const read = (markdown) => markdown.replaceAll(" ", " ");

// --- The files the tools write ----------------------------------------------------

/** pytest: one passed, one slow with a name that needs escaping, one failed, one skipped, one expected failure. */
const PYTEST = `<?xml version="1.0" encoding="utf-8"?><testsuites name="pytest tests"><testsuite name="pytest" errors="0" failures="1" skipped="2" tests="5" time="12.5" timestamp="2026-10-07T10:00:00" hostname="SECRET-HOST"><testcase classname="tests.test_units" name="test_round_trip" time="0.250" /><testcase classname="tests.test_units" name="test_slow[a&gt;b|c]" time="9.5" /><testcase classname="tests.test_drive" name="test_fault" time="1.0"><failure message="assert SECRET-IN-MESSAGE == 2">tests/test_drive.py:12: AssertionError &lt;SECRET-IN-BODY&gt;</failure><system-out><![CDATA[SECRET-IN-OUTPUT <testcase name="ghost" time="99"/>]]></system-out></testcase><testcase classname="tests.test_hw" name="test_needs_bench" time="0.0"><skipped type="pytest.skip" message="no bench">reason</skipped></testcase><testcase classname="tests.test_known" name="test_s07" time="0.5"><skipped type="pytest.xfail" message="known" /></testcase></testsuite></testsuites>`;

/** pytest, a second process of the same suite: two passed. */
const PYTEST_OTHER = `<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest" errors="0" failures="0" skipped="0" tests="2" time="3.0"><testcase classname="tests.test_clock" name="test_now" time="1.5" /><testcase classname="tests.test_clock" name="test_later" time="1.5" /></testsuite></testsuites>`;

/** vitest: a file with two tests, one of them skipped. */
const VITEST = `<?xml version="1.0" encoding="UTF-8" ?>
<testsuites name="vitest tests" tests="3" failures="0" errors="0" time="0.5">
    <testsuite name="lib/training.test.ts" timestamp="2026-10-07T11:34:53.837Z" hostname="SECRET-HOST" tests="3" failures="0" errors="0" skipped="1" time="0.4">
        <testcase classname="lib/training.test.ts" name="isFresh &gt; is fresh at 89 s" time="0.25">
        </testcase>
        <testcase classname="lib/training.test.ts" name="isFresh &gt; is stale at 90 s" time="0.15">
        </testcase>
        <testcase classname="lib/training.test.ts" name="isFresh &gt; later" time="0">
            <skipped/>
        </testcase>
    </testsuite>
</testsuites>
`;

/** `node --test`: tests outside and inside a suite, a failure, and the counts it adds as comments. */
const NODE = `<?xml version="1.0" encoding="utf-8"?>
<testsuites>
\t<testcase name="refuses a missing catalogue" time="0.078693" classname="test"/>
\t<testsuite name="the path rule" time="0.5" disabled="0" errors="0" tests="2" failures="1" skipped="0" hostname="SECRET-HOST">
\t\t<testcase name="keeps the site gates" time="0.2" classname="test"/>
\t\t<testcase name="keeps the Python gates" time="0.3" classname="test" file="/home/runner/work/x.test.mjs">
\t\t\t<failure type="testCodeFailure" message="SECRET-IN-NODE-MESSAGE">
[Error [ERR_TEST_FAILURE]: SECRET-IN-NODE-BODY]
\t\t\t</failure>
\t\t</testcase>
\t</testsuite>
\t<!-- tests 3 -->
\t<!-- pass 2 -->
\t<!-- <testcase name="in a comment" time="5"/> -->
</testsuites>
`;

/**
 * `coverage json` of coverage.py.
 * @param {readonly (readonly [string, number, number, number, number])[]} files name, statements, covered, branches, covered
 */
const coveragePy = (files) =>
  JSON.stringify({
    meta: { version: "7.16.1", branch_coverage: true },
    files: Object.fromEntries(
      files.map(([file, statements, lines, branches, taken]) => [
        file,
        {
          executed_lines: [],
          summary: {
            covered_lines: lines,
            num_statements: statements,
            num_branches: branches,
            covered_branches: taken,
            missing_lines: statements - lines,
            missing_branches: branches - taken,
          },
        },
      ]),
    ),
    totals: { percent_covered: 0 },
  });

/**
 * One source file in a `coverage-final.json` of vitest: three statements on
 * lines 1, 2 and 2, one branch with two arms on line 2.
 * @param {string} path @param {readonly [number, number, number]} statements hits @param {readonly [number, number]} arms hits
 */
const istanbulFile = (path, statements, arms) => ({
  path,
  statementMap: {
    0: { start: { line: 1, column: 0 }, end: { line: 1, column: 9 } },
    1: { start: { line: 2, column: 0 }, end: { line: 2, column: 4 } },
    2: { start: { line: 2, column: 6 }, end: { line: 2, column: 9 } },
  },
  fnMap: {},
  branchMap: {
    0: {
      type: "if",
      line: 2,
      loc: { start: { line: 2, column: 0 }, end: { line: 2, column: 9 } },
      locations: [{}, {}],
    },
  },
  s: { 0: statements[0], 1: statements[1], 2: statements[2] },
  f: {},
  b: { 0: [...arms] },
});

/** @param {readonly ReturnType<typeof istanbulFile>[]} files */
const istanbul = (files) => JSON.stringify(Object.fromEntries(files.map((file) => [file.path, file])));

// --- Reading what the tools write ---------------------------------------------------

test("test counts are read from the JUnit files of pytest, of vitest and of node --test", () => {
  const pytest = parseJunit(PYTEST) ?? [];
  assert.deepEqual(
    pytest.map(({ name, state }) => `${state} ${name}`),
    [
      "passed tests.test_units :: test_round_trip",
      "passed tests.test_units :: test_slow[a>b|c]",
      "failed tests.test_drive :: test_fault",
      "skipped tests.test_hw :: test_needs_bench",
      "skipped tests.test_known :: test_s07",
    ],
  );
  assert.deepEqual(
    pytest.map(({ expectedFailure }) => expectedFailure),
    [false, false, false, false, true],
  );
  assert.deepEqual(
    (parseJunit(VITEST) ?? []).map(({ name, state, seconds }) => [name, state, seconds]),
    [
      ["lib/training.test.ts :: isFresh > is fresh at 89 s", "passed", 0.25],
      ["lib/training.test.ts :: isFresh > is stale at 90 s", "passed", 0.15],
      ["lib/training.test.ts :: isFresh > later", "skipped", 0],
    ],
  );
  // What a comment or a test's output holds is not a test, and the class "test" of node says nothing.
  assert.deepEqual(
    (parseJunit(NODE) ?? []).map(({ name, state }) => `${state} ${name}`),
    ["passed refuses a missing catalogue", "passed keeps the site gates", "failed keeps the Python gates"],
  );
  assert.equal(parseJunit("PASSED 3 tests in 2.1s"), undefined, "a log is not a JUnit file");
  assert.equal(parseJunit(""), undefined);
});

test("a suite run by several processes adds their files up, and has no numbers if one is missing", (context) => {
  const directory = scratch(context);
  const files = [write(directory, "junit-0.xml", PYTEST), write(directory, "junit-1.xml", PYTEST_OTHER)];
  const numbers = suiteNumbers(files, 2);
  assert.deepEqual(
    { ...numbers, slowest: undefined, failed_tests: undefined },
    {
      tests: 7,
      passed: 4,
      failed: 1,
      skipped: 2,
      expected_failures: 1,
      duration_s: 14.25,
      files: 2,
      slowest: undefined,
      failed_tests: undefined,
    },
  );
  assert.deepEqual(numbers?.failed_tests, ["tests.test_drive :: test_fault"]);
  // The slowest first; a test of less than a tenth of a second is not listed.
  assert.deepEqual(
    numbers?.slowest.map(({ seconds }) => seconds),
    [9.5, 1.5, 1.5, 1, 0.5, 0.25],
  );
  // Four processes were started and two files are there: the counts would be too low.
  assert.equal(suiteNumbers(files, 4), undefined);
  assert.equal(suiteNumbers([], undefined), undefined);
  assert.equal(suiteNumbers([...files, join(directory, "junit-2.xml")], 3), undefined, "a file that is not there");
  assert.equal(suiteNumbers([write(directory, "junit-9.xml", "Killed")]), undefined, "a file that is not JUnit");
});

test("the coverage of a Python project is read from coverage json, lines and branches apart", (context) => {
  const directory = scratch(context);
  const report = write(
    directory,
    "coverage-all.json",
    coveragePy([
      ["src/units.py", 40, 40, 10, 10],
      ["src/web/ws.py", 100, 25, 20, 4],
      ["src/web/app.py", 60, 30, 10, 5],
      ["src/__init__.py", 0, 0, 0, 0],
    ]),
  );
  const coverage = pythonCoverage(report);
  assert.deepEqual(coverage?.lines, { covered: 95, total: 200 });
  assert.deepEqual(coverage?.branches, { covered: 19, total: 40 });
  assert.equal(coverage?.files, 4);
  // The least covered first; a file with nothing missing is not listed.
  assert.deepEqual(
    coverage?.least_covered.map(({ file }) => file),
    ["src/web/ws.py", "src/web/app.py"],
  );
  assert.equal(pythonCoverage(join(directory, "absent.json")), undefined);
  assert.equal(pythonCoverage(write(directory, "broken.json", "{")), undefined);
  assert.equal(pythonCoverage(write(directory, "other.json", '{"files": {}, "totals": {}}')), undefined);
});

test("the coverage of the site counts a line once, covered if either of its two suites covers it", (context) => {
  const directory = scratch(context);
  const root = "/home/runner/work/Anheart/Anheart";
  const lib = write(
    directory,
    "lib.json",
    istanbul([
      istanbulFile(`${root}/lib/training.ts`, [3, 0, 0], [0, 0]),
      istanbulFile(`${root}/components/Card.tsx`, [0, 0, 0], [0, 0]),
    ]),
  );
  const components = write(
    directory,
    "components.json",
    istanbul([
      istanbulFile(`${root}/lib/training.ts`, [0, 0, 2], [2, 0]),
      istanbulFile(`${root}/components/Card.tsx`, [0, 0, 0], [0, 0]),
    ]),
  );
  // Each file: two lines (two statements start on line 2) and two arms.
  assert.deepEqual(istanbulCoverage([lib], root)?.lines, { covered: 1, total: 4 });
  assert.deepEqual(istanbulCoverage([components], root)?.lines, { covered: 1, total: 4 });
  const both = istanbulCoverage([lib, components], root);
  assert.deepEqual(both?.lines, { covered: 2, total: 4 });
  assert.deepEqual(both?.branches, { covered: 1, total: 4 });
  assert.deepEqual(
    both?.least_covered.map(({ file }) => file),
    ["components/Card.tsx", "lib/training.ts"],
  );
  assert.equal(istanbulCoverage([lib, join(directory, "absent.json")], root), undefined);
  assert.equal(istanbulCoverage([], root), undefined);
});

test("a percentage is never rounded up, and a duration reads in minutes", () => {
  assert.equal(percent({ covered: 9999, total: 10000 }), "99,9 %");
  assert.equal(percent({ covered: 10000, total: 10000 }), "100 %");
  assert.equal(percent({ covered: 966, total: 1000 }), "96,6 %");
  assert.equal(percent({ covered: 0, total: 12 }), "0,0 %");
  assert.equal(percent({ covered: 0, total: 0 }), "sans objet");
  assert.equal(percent(undefined), "indisponible");
  assert.equal(duration(3.84), "3,8 s");
  assert.equal(duration(130), "2 min 10 s");
  assert.equal(duration(3725), "1 h 02 min");
});

test("a name cannot break a table nor bring markup into the summary", () => {
  assert.equal(
    cell('test[a|b] <img src=x onerror="alert(1)">\n&amp;'),
    '<code>test[a&#124;b] &lt;img src=x onerror="alert(1)"&gt; &amp;amp;</code>',
  );
  assert.ok(cell("x".repeat(500)).length < 200);
});

// --- The gate scripts ----------------------------------------------------------------

const GATE_SCRIPTS = ["raspberry-pi/scripts/check.sh", "simulation/scripts/check.sh"];

/** The part of a gate script that records the stages: from the list of failures to the end of `stage`. */
const stageBlock = (/** @type {string} */ path) => {
  const block = /^failures=\(\)\n[\s\S]*?^stage\(\) \{\n[\s\S]*?^\}\n/m.exec(
    readFileSync(join(ROOT, path), "utf8"),
  )?.[0];
  assert.ok(block, `${path}: no stage function`);
  return block;
};

/** Run stages through the `stage` function of a gate script, as written. @param {string} path @param {string} report */
const runStages = (path, report) =>
  spawnSync(
    "bash",
    [
      "--noprofile",
      "--norc",
      "-c",
      `set -uo pipefail\nREPORT="$1"\n${stageBlock(path)}\n` +
        'stage "lint (ruff)" true\nstage "types (mypy, strict)" false\nstage "tests + 100% branch coverage" true\n' +
        'echo "failures: ${failures[*]}"\n',
      "bash",
      report,
    ],
    { encoding: "utf8", env: { PATH: process.env.PATH ?? "" } },
  );

test("a gate script writes down how each stage ended, and decides the same with or without a place to write", (context) => {
  for (const path of GATE_SCRIPTS) {
    const directory = join(scratch(context), "not there yet");
    const kept = runStages(path, directory);
    assert.equal(kept.status, 0, kept.stderr);
    assert.match(kept.stdout, /^failures: types \(mypy, strict\)$/m, path);
    assert.equal(
      readFileSync(join(directory, "stages.tsv"), "utf8"),
      "passed\tlint (ruff)\nfailed\ttypes (mypy, strict)\npassed\ttests + 100% branch coverage\n",
      path,
    );
    // Asked to write under a file: it says so once and judges the stages as before.
    const nowhere = runStages(path, join(directory, "stages.tsv", "below a file"));
    assert.equal(nowhere.status, 0, nowhere.stderr);
    assert.match(nowhere.stdout, /^failures: types \(mypy, strict\)$/m, path);
    assert.match(nowhere.stderr, /QUALITY_REPORT_DIR: nothing can be kept in /, path);
    // Not asked: nothing is written anywhere.
    const plain = runStages(path, "");
    assert.match(plain.stdout, /^failures: types \(mypy, strict\)$/m, path);
    assert.equal(plain.stderr.replace("FAILED: types (mypy, strict)\n", ""), "", path);
  }
});

test("every lint and type stage of the gate scripts is one the report recognises", () => {
  for (const [path, project] of /** @type {const} */ ([
    [GATE_SCRIPTS[0], "pi"],
    [GATE_SCRIPTS[1], "simulation"],
  ])) {
    const source = readFileSync(join(ROOT, path ?? ""), "utf8");
    const stages = [...source.matchAll(/^ *stage "([^"$]+)"/gm)].map(([, name]) => name ?? "");
    assert.ok(stages.length >= 5, `${path}: the stages were not read`);
    const checks = stageChecks(stages.map((name) => `passed\t${name}`).join("\n"), project, "the gate");
    const found = (/** @type {string} */ kind, /** @type {string} */ whose) =>
      checks.filter((check) => check.kind === kind && check.project === whose).map((check) => check.name);
    assert.deepEqual(found("lint", project), ["lint (ruff)", "format (ruff)"], path);
    assert.deepEqual(found("types", project), ["types (basedpyright, strict, zero Any)", "types (mypy, strict)"], path);
    // A stage that is a lint or a type check by its name and is not read would go unreported.
    const named = stages.filter((name) => /lint|format|types/.test(name));
    assert.equal(checks.length, named.length, `${path}: ${named.join(", ")}`);
  }
  // The helpers of the gates, checked by the Pi gate, count for the scripts.
  const helpers = stageChecks(
    "passed\tgate helpers: lint (ruff)\nfailed\tgate helpers: types (mypy)\n",
    "pi",
    "pi-gate",
  );
  assert.deepEqual(
    helpers.map(({ kind, project, state }) => [kind, project, state]),
    [
      ["lint", "scripts", "passed"],
      ["types", "scripts", "failed"],
    ],
  );
  assert.deepEqual(stageChecks("passed\ttests + 100% branch coverage\nrunning\tlint (ruff)\n", "pi", "pi-gate"), []);
  assert.deepEqual(stageChecks(undefined, "pi", "pi-gate"), []);
});

// --- The numbers of one job ------------------------------------------------------------

/** What the Pi gate leaves in its report directory. @param {string} directory */
function piGateLeft(directory) {
  write(directory, "junit-0.xml", PYTEST);
  write(directory, "junit-1.xml", PYTEST_OTHER);
  write(directory, "junit-gate-runner.xml", PYTEST_OTHER);
  write(
    directory,
    "stages.tsv",
    [
      "passed\tlint (ruff)",
      "passed\tformat (ruff)",
      "passed\ttypes (basedpyright, strict, zero Any)",
      "failed\ttypes (mypy, strict)",
      "passed\tgate helpers: lint (ruff)",
      "passed\tgate helpers: types (mypy)",
      "passed\tparallel runner (its own tests)",
      "failed\ttests + 100% branch coverage",
      "",
    ].join("\n"),
  );
  write(directory, "coverage-gate.json", coveragePy([["src/training/safety.py", 300, 300, 120, 120]]));
  write(
    directory,
    "coverage-all.json",
    coveragePy([
      ["src/training/safety.py", 300, 300, 120, 120],
      ["src/web/ws.py", 100, 25, 20, 4],
    ]),
  );
}

test("the Pi gate reports its tests, its whole coverage, its safety chain apart, and its checks", (context) => {
  const directory = scratch(context);
  piGateLeft(directory);
  const part = buildPart("pi", { dir: directory, shares: 2, root: ROOT, env: {} });
  assert.equal(part.schema, SCHEMA_VERSION);
  assert.equal(part.suites["pi-pytest"]?.tests, 7);
  assert.equal(part.suites["scripts-gate-runner"]?.tests, 2);
  assert.deepEqual(part.coverage.pi?.lines, { covered: 325, total: 400 });
  assert.deepEqual(part.coverage.pi?.branches, { covered: 124, total: 140 });
  assert.deepEqual(part.coverage["pi-safety-chain"]?.lines, { covered: 300, total: 300 });
  assert.deepEqual(part.coverage["pi-safety-chain"]?.branches, { covered: 120, total: 120 });
  assert.deepEqual(
    part.checks.map(({ kind, project, job, state }) => `${kind} ${project} ${job} ${state}`),
    [
      "lint pi pi-gate passed",
      "lint pi pi-gate passed",
      "types pi pi-gate passed",
      "types pi pi-gate failed",
      "lint scripts pi-gate passed",
      "types scripts pi-gate passed",
    ],
  );

  const summary = renderPart(part);
  assert.match(summary, /^<details><summary><b>Détail de la qualité : console du Pi<\/b><\/summary>$/m);
  assert.match(summary, /^\| tests Python de la console \| pytest \| 7 \| 4 \| 1 \| 2 \(dont 1 xfail\) \| 14,3 s \|$/m);
  assert.match(
    summary,
    /^\| tout `raspberry-pi\/src\/` \| 2 \| 81,2 % \(325 sur 400\) \| 88,5 % \(124 sur 140\) \| aucun \|$/m,
  );
  assert.match(
    summary,
    /^\| chaîne de sécurité du Pi \| 1 \| 100 % \(300 sur 300\) \| 100 % \(120 sur 120\) \| 100 % \|$/m,
  );
  // The least covered files and the slowest tests, EX-2.
  assert.match(summary, /^Fichiers les moins couverts \(tout `raspberry-pi\/src\/`\) :$/m);
  assert.match(summary, /^\| <code>src\/web\/ws\.py<\/code> \| 25,0 % \(25 sur 100\) \| 20,0 % \(4 sur 20\) \|$/m);
  assert.match(summary, /^Tests les plus lents \(tests Python de la console\) :$/m);
  assert.match(summary, /^\| <code>tests\.test_units :: test_slow\[a&gt;b&#124;c\]<\/code> \| 9,5 s \|$/m);
  assert.match(summary, /^- <code>tests\.test_drive :: test_fault<\/code>$/m, "a failed test is named");
});

test("a job summary holds names, counts and durations, and nothing a test printed or said when failing", (context) => {
  const directory = scratch(context);
  piGateLeft(directory);
  const part = buildPart("pi", { dir: directory, shares: 2, root: ROOT, env: {} });
  const published = JSON.stringify(part) + renderPart(part);
  assert.doesNotMatch(published, /SECRET|ghost|AssertionError|2026-10-07/);
  const scripts = buildPart("site", {
    dir: write(directory, "site/pi-panel.xml", NODE).replace(/pi-panel\.xml$/, ""),
    root: ROOT,
    env: {},
  });
  assert.equal(scripts.suites["pi-panel"]?.failed, 1);
  assert.doesNotMatch(
    JSON.stringify(scripts) + renderPart(scripts),
    /SECRET|ERR_TEST_FAILURE|in a comment|\/home\/runner/,
  );
});

test("the simulation gate reports the battery of every job, its coverage, and the scenarios of the synthetic report", (context) => {
  const directory = scratch(context);
  const evidence = join(directory, "evidence");
  const stages = "passed\tlint (ruff)\npassed\tformat (ruff)\npassed\ttypes (basedpyright, strict, zero Any)\n";
  write(evidence, "quality-0-0/junit-0.xml", PYTEST_OTHER);
  write(evidence, "quality-0-0/stages.tsv", `${stages}passed\ttypes (mypy, strict)\n`);
  write(evidence, "quality-1-2/junit-1.xml", PYTEST);
  write(evidence, "quality-1-2/junit-2.xml", PYTEST_OTHER);
  // The same checks in each job of the battery: one of them failed in this one.
  write(evidence, "quality-1-2/stages.tsv", `${stages}failed\ttypes (mypy, strict)\n`);
  write(evidence, "collected-0.txt", "not a part of the report");
  write(directory, "left/coverage-gate.json", coveragePy([["harness.py", 500, 500, 200, 200]]));
  const report = write(
    directory,
    "report.json",
    JSON.stringify({
      wall_s: 12.5,
      counts: { PASS: 2, XFAIL: 1 },
      runs: [
        { name: "S01", group: "scenarios", status: "PASS" },
        { name: "P01", group: "cohort", status: "PASS" },
        { name: "S07_auto_jog", group: "scenarios", status: "XFAIL" },
      ],
    }),
  );
  const options = { dir: join(directory, "left"), parts: evidence, scenarios: report, root: ROOT, env: {} };
  const part = buildPart("simulation", { ...options, shares: 3 });
  assert.equal(part.suites["simulation-battery"]?.tests, 9);
  assert.equal(part.suites["simulation-battery"]?.files, 3);
  assert.deepEqual(part.coverage.simulation?.branches, { covered: 200, total: 200 });
  assert.deepEqual(part.scenarios, {
    runs: 3,
    by_status: { PASS: 2, XFAIL: 1 },
    by_group: { scenarios: 2, cohort: 1 },
  });
  assert.deepEqual(
    part.checks.map(({ kind, name, state }) => `${kind} ${state} ${name}`),
    [
      "lint passed lint (ruff)",
      "lint passed format (ruff)",
      "types passed types (basedpyright, strict, zero Any)",
      "types failed types (mypy, strict)",
    ],
  );
  assert.match(renderPart(part), /^3 scénarios joués par `simulation\.quick --all` : 2 PASS, 1 XFAIL\.$/m);
  // The battery is cut into 13 shares and three files arrived: no count is given for it.
  assert.equal(buildPart("simulation", { ...options, shares: 13 }).suites["simulation-battery"], undefined);
  assert.equal(buildPart("simulation", { ...options, scenarios: join(directory, "absent.json") }).scenarios, undefined);
});

test("Convex and the site report their tests, their coverage without threshold and the outcome of their checks", (context) => {
  const directory = scratch(context);
  write(directory, "convex.xml", VITEST);
  write(
    directory,
    "coverage-convex/coverage-final.json",
    istanbul([istanbulFile(`${ROOT}/convex/training.ts`, [1, 1, 0], [1, 0])]),
  );
  const convex = buildPart("convex", { dir: directory, root: ROOT, env: { TYPES_OUTCOME: "success" } });
  assert.equal(convex.suites.convex?.tests, 3);
  assert.equal(convex.suites.convex?.skipped, 1);
  assert.deepEqual(convex.coverage.convex?.least_covered, [
    { file: "convex/training.ts", lines: { covered: 2, total: 2 }, branches: { covered: 1, total: 2 } },
  ]);
  assert.deepEqual(convex.checks, [
    { kind: "types", project: "convex", job: "convex-tests", name: "tsc -p convex/tsconfig.json", state: "passed" },
  ]);
  assert.match(renderPart(convex), /^\| `convex\/` \| 1 \| 100 % \(2 sur 2\) \| 50,0 % \(1 sur 2\) \| aucun \|$/m);

  write(directory, "site-lib.xml", VITEST);
  write(directory, "site-components.xml", VITEST);
  write(
    directory,
    "coverage-site-lib/coverage-final.json",
    istanbul([istanbulFile(`${ROOT}/lib/training.ts`, [1, 0, 0], [0, 0])]),
  );
  // The type check failed, so the job stopped: the lint did not run, and one of the two coverage files is not there.
  const site = buildPart("site", {
    dir: directory,
    root: ROOT,
    env: { TYPES_OUTCOME: "failure", LINT_OUTCOME: "skipped" },
  });
  assert.deepEqual(Object.keys(site.suites), ["site-lib", "site-components"]);
  assert.deepEqual(site.coverage, {}, "a total over one of the two suites would read lower than what was measured");
  assert.deepEqual(
    site.checks.map(({ kind, project, state }) => `${kind} ${project} ${state}`),
    ["types site failed", "lint site not_run", "lint convex not_run", "lint scripts not_run"],
  );
  assert.throws(() => buildPart("audit", { dir: directory, root: ROOT, env: {} }), /unknown part/);
});

// --- The report of the run ---------------------------------------------------------------

/** @param {number} tests @param {number} [failed] @param {number} [skipped] @param {number} [seconds] */
const numbers = (tests, failed = 0, skipped = 0, seconds = tests / 10) => ({
  tests,
  passed: tests - failed - skipped,
  failed,
  skipped,
  expected_failures: 0,
  duration_s: seconds,
  files: 1,
  slowest: [],
  failed_tests: [],
});

/** @param {number} lines @param {number} of @param {number} branches @param {number} arms */
const covered = (lines, of, branches, arms) => ({
  lines: { covered: lines, total: of },
  branches: { covered: branches, total: arms },
  files: 3,
  least_covered: [],
});

/** @param {"lint" | "types"} kind @param {string} project @param {string} job @param {"passed" | "failed" | "not_run"} [state] */
const check = (kind, project, job, state = "passed") => ({ kind, project, job, name: `${kind} of ${project}`, state });

/** What every job leaves on a run where every gate ran and passed. */
const everyPart = () => ({
  pi: {
    schema: SCHEMA_VERSION,
    part: "pi",
    suites: { "pi-pytest": numbers(3208, 0, 8, 3130), "scripts-gate-runner": numbers(102, 0, 0, 33) },
    coverage: { pi: covered(9120, 10000, 2640, 3000), "pi-safety-chain": covered(7000, 7000, 2000, 2000) },
    checks: [
      check("lint", "pi", "pi-gate"),
      check("types", "pi", "pi-gate"),
      check("lint", "scripts", "pi-gate"),
      check("types", "scripts", "pi-gate"),
    ],
  },
  simulation: {
    schema: SCHEMA_VERSION,
    part: "simulation",
    suites: { "simulation-battery": { ...numbers(1024, 0, 1, 2400), expected_failures: 1 } },
    coverage: { simulation: covered(4000, 4000, 1500, 1500) },
    checks: [check("lint", "simulation", "simulation-gate"), check("types", "simulation", "simulation-gate")],
    scenarios: {
      runs: 318,
      by_status: { PASS: 310, XFAIL: 1, SKIPPED: 7 },
      by_group: { failures: 199, cohort: 90, scenarios: 29 },
    },
  },
  convex: {
    schema: SCHEMA_VERSION,
    part: "convex",
    suites: { convex: numbers(925, 0, 0, 3.8) },
    coverage: { convex: covered(1356, 1403, 863, 988) },
    checks: [check("types", "convex", "convex-tests")],
  },
  site: {
    schema: SCHEMA_VERSION,
    part: "site",
    suites: { "site-lib": numbers(75), "site-components": numbers(61), "pi-panel": numbers(40) },
    coverage: { site: covered(193, 1211, 230, 1125) },
    checks: [
      check("types", "site", "web"),
      check("lint", "site", "web"),
      check("lint", "convex", "web"),
      check("lint", "scripts", "web"),
    ],
  },
});

/** The JUnit files the jobs that only run a test file of the scripts leave. */
const everyScript = () => ({
  "scripts-ci": numbers(120),
  "scripts-dependency-guard": numbers(8),
  "scripts-gitleaks-fixture": numbers(6),
  "scripts-men": numbers(14),
});

/** The `needs` context of the report job. @param {Record<string, string>} [results] @param {Record<string, string>} [rule] */
const needs = (results = {}, rule = { python: "true", node: "true" }) =>
  Object.fromEntries(
    ["changes", ...GATES].map((job) => [
      job,
      { result: results[job] ?? "success", outputs: job === "changes" ? rule : {} },
    ]),
  );

const RUN = {
  id: "37614000000",
  attempt: 1,
  event: "pull_request",
  sha: "0123456789abcdef",
  pull_request: 40,
  url: null,
};

/** The line of a project in the table. @param {string} markdown @param {string} label */
const line = (markdown, label) => {
  const found = markdown.split("\n").filter((text) => text.startsWith(`| ${label}`));
  assert.ok(found[0] !== undefined, `no line "${label}"`);
  return found[0].split(" | ").map((text) => text.replaceAll(/^\| | \|$/g, ""));
};

const HEADER = [
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
];

test("the table of the run has one line per project and the columns of the ticket", () => {
  const report = buildReport({
    parts: everyPart(),
    junit: everyScript(),
    needs: needs(),
    event: "pull_request",
    run: RUN,
  });
  const markdown = read(renderReport(report));
  const lines = markdown.split("\n");
  assert.equal(lines[0], "## Rapport de qualité");
  assert.equal(lines[2], "**5 583 tests lancés, 0 en échec.** 6 gates réussies sur 6.");
  assert.equal(lines[4], `| ${HEADER.join(" | ")} |`);
  assert.deepEqual(
    lines.slice(6, 6 + PROJECTS.length).map((text) => text.split(" | ")[0]),
    [
      "| Console du Pi",
      "| Simulation",
      "| Convex",
      "| Site (`lib/`, `hooks/`, `components/`)",
      "| Scripts (CI et release)",
    ],
  );
  assert.deepEqual(line(markdown, "Console du Pi"), [
    "Console du Pi",
    "3 248",
    "3 240",
    "0",
    "8",
    "52 min 14 s",
    "91,2 %",
    "88,0 %",
    "✅ réussi",
    "✅ réussi",
    "✅ réussie : `pi-gate`",
  ]);
  assert.deepEqual(line(markdown, "Simulation").slice(1, 8), [
    "1 024",
    "1 023",
    "0",
    "1",
    "40 min 00 s",
    "100 %",
    "100 %",
  ]);
  assert.deepEqual(line(markdown, "Convex").slice(1), [
    "925",
    "925",
    "0",
    "0",
    "3,8 s",
    "96,6 %",
    "87,3 %",
    "✅ réussi",
    "✅ réussi",
    "✅ réussie : `convex-tests`",
  ]);
  assert.deepEqual(line(markdown, "Site").slice(6, 8), ["15,9 %", "20,4 %"]);
  // No tool measures the coverage of the scripts: it is said, not shown as zero.
  assert.deepEqual(line(markdown, "Scripts").slice(1), [
    "250",
    "250",
    "0",
    "0",
    "47,8 s",
    "non mesurée",
    "non mesurée",
    "✅ réussi",
    "✅ réussi",
    "✅ réussie : `changes`, `audit`, `docs`",
  ]);
  assert.doesNotMatch(markdown, /¹/);
  // EX-4: the safety chain apart from the whole, each threshold with whether it holds, the scenarios, the battery.
  assert.match(
    markdown,
    /^\| Couverture, chaîne de sécurité du Pi \| lignes 100 % \(7 000 sur 7 000\), branches 100 % \(2 000 sur 2 000\) \| 100 % exigé par `pi-gate` : ✅ tenu \|$/m,
  );
  assert.match(
    markdown,
    /^\| Couverture, code de la simulation \| .* \| 100 % exigé par `simulation-gate` : ✅ tenu \|$/m,
  );
  assert.match(
    markdown,
    /^\| Batterie de simulation \| 1 024 tests : 1 023 réussis, 0 en échec, 1 ignoré \(dont 1 xfail\) \| /m,
  );
  assert.match(
    markdown,
    /^\| Scénarios de simulation \| 318 scénarios joués par `simulation\.quick --all` : 310 PASS, 7 SKIPPED, 1 XFAIL \| /m,
  );
  // Every suite of the catalogue has its line in the detail.
  for (const suite of SUITES)
    assert.ok(markdown.includes(`| ${suite.label} | ${suite.runner} | \`${suite.job}\` |`), suite.id);
  assert.match(
    markdown,
    /^<sub>PR #40 · commit `0123456` · événement `pull_request` · exécution 37614000000, tentative 1<\/sub>$/m,
  );
});

test("a gate the path rule skipped reads « sautée », never zero", () => {
  // A pull request that only changes the site: the Python gates are skipped.
  const { convex, site } = everyPart();
  const report = buildReport({
    parts: { convex, site },
    junit: everyScript(),
    needs: needs({ "pi-gate": "skipped", "simulation-gate": "skipped" }, { python: "false", node: "true" }),
    event: "pull_request",
    run: RUN,
  });
  const markdown = read(renderReport(report));
  for (const label of ["Console du Pi", "Simulation"]) {
    const cells = line(markdown, label);
    assert.deepEqual(
      cells.slice(1, 10),
      Array.from({ length: 9 }, () => "sautée"),
      label,
    );
    assert.match(cells[10] ?? "", /^⏭️ sautée : `(pi|simulation)-gate`$/, label);
  }
  assert.match(
    markdown,
    /4 gates réussies sur 6, 2 sautées par la règle de chemins \(`pi-gate`, `simulation-gate`\)\.$/m,
  );
  assert.match(markdown, /^\| Couverture, chaîne de sécurité du Pi \| sautée \| 100 % exigé par `pi-gate` \|$/m);
  assert.match(markdown, /^\| Batterie de simulation \| sautée \| /m);
  assert.match(markdown, /^\| Scénarios de simulation \| sautée \| /m);
  assert.match(
    markdown,
    /^\| Console du Pi \| tests Python de la console \| pytest \| `pi-gate` \| sautée \| sautée \| sautée \| sautée \| sautée \|$/m,
  );
  // What did run is counted: the scripts lack the tests and the checks the Pi gate runs for them, and say so.
  const scripts = line(markdown, "Scripts");
  assert.deepEqual([scripts[1], scripts[8], scripts[9]], ["148 ¹", "✅ réussi (partiel)", "sautée"]);
  assert.match(markdown, /^¹ Une suite de ce projet n'a pas de chiffres dans cette exécution/m);
  assert.deepEqual(line(markdown, "Convex").slice(8), ["✅ réussi", "✅ réussi", "✅ réussie : `convex-tests`"]);
  const json = report.projects.find(({ id }) => id === "pi");
  assert.deepEqual(
    [json?.gate.state, json?.coverage, json?.coverage_state, json?.lint.state],
    ["skipped", null, "skipped", "skipped"],
  );
});

test("a skipped job is « sautée » only when the path rule ran on a pull request and said so", () => {
  const skipped = { "pi-gate": "skipped", "simulation-gate": "skipped", "convex-tests": "skipped", web: "skipped" };
  const said = jobStates(needs(skipped, { python: "false", node: "false" }), "pull_request");
  assert.deepEqual(
    [said["pi-gate"], said["simulation-gate"], said["convex-tests"], said.web, said.audit, said.changes],
    ["skipped", "skipped", "skipped", "skipped", "passed", "passed"],
  );
  // The rule answered for the site only.
  const partly = jobStates(needs(skipped, { python: "true", node: "false" }), "pull_request");
  assert.deepEqual([partly["pi-gate"], partly.web], ["not_run", "skipped"]);
  // A push never consults the rule; a rule that failed or was cancelled decided nothing.
  assert.equal(jobStates(needs(skipped, { python: "false", node: "false" }), "push")["pi-gate"], "not_run");
  assert.equal(
    jobStates(needs({ ...skipped, changes: "failure" }, { python: "false" }), "pull_request")["pi-gate"],
    "not_run",
  );
  assert.equal(jobStates(needs({ ...skipped, changes: "cancelled" }, {}), "pull_request").web, "not_run");
  // `audit` and `docs` wait for no rule: skipped, they did not run.
  assert.equal(
    jobStates(needs({ audit: "skipped" }, { python: "false", node: "false" }), "pull_request").audit,
    "not_run",
  );
  assert.deepEqual(
    Object.values(jobStates(needs({ "pi-gate": "failure", web: "cancelled", docs: "" }), "pull_request")),
    ["passed", "failed", "passed", "passed", "cancelled", "passed", "unknown"],
  );
  assert.deepEqual(new Set(Object.values(jobStates(undefined, "pull_request"))), new Set(["unknown"]));
  assert.deepEqual(new Set(Object.values(jobStates("not an object", "push"))), new Set(["unknown"]));
});

test("a gate that failed keeps its numbers and reads « échec »", () => {
  const parts = everyPart();
  parts.pi.suites["pi-pytest"] = numbers(3208, 3, 8, 3130);
  parts.pi.coverage["pi-safety-chain"] = covered(6990, 7000, 1990, 2000);
  parts.pi.checks[1] = check("types", "pi", "pi-gate", "failed");
  const report = buildReport({
    parts,
    junit: everyScript(),
    needs: needs({ "pi-gate": "failure" }),
    event: "push",
    run: RUN,
  });
  const markdown = read(renderReport(report));
  assert.deepEqual(line(markdown, "Console du Pi"), [
    "Console du Pi",
    "3 248",
    "3 237",
    "**3**",
    "8",
    "52 min 14 s",
    "91,2 %",
    "88,0 %",
    "✅ réussi",
    "❌ échec",
    "❌ échec : `pi-gate`",
  ]);
  assert.match(
    markdown,
    /^\*\*5 583 tests lancés, 3 en échec\.\*\* 5 gates réussies sur 6, 1 en échec \(`pi-gate`\)\.$/m,
  );
  assert.match(
    markdown,
    /\| lignes 99,8 % \(6 990 sur 7 000\), branches 99,5 % \(1 990 sur 2 000\) \| 100 % exigé par `pi-gate` : ❌ non tenu \|$/m,
  );
});

test("a job that left no artifact reads « indisponible », and one that did not run « non lancé »", () => {
  const { pi, site } = everyPart();
  const junit = { "scripts-ci": numbers(120), "scripts-men": numbers(14) };
  // The artifacts of `simulation-gate`, of `convex-tests` and of `audit` are missing; `convex-tests` was cancelled.
  const states = needs({ "convex-tests": "cancelled", "simulation-gate": "failure" });
  const report = buildReport({ parts: { pi, site }, junit, needs: states, event: "push", run: RUN });
  const markdown = read(renderReport(report));
  assert.deepEqual(line(markdown, "Simulation").slice(1), [
    ...Array.from({ length: 9 }, () => "indisponible"),
    "❌ échec : `simulation-gate`",
  ]);
  assert.deepEqual(line(markdown, "Convex").slice(1), [
    ...Array.from({ length: 7 }, () => "non lancé"),
    "✅ réussi",
    "non lancé",
    "⚪ annulée : `convex-tests`",
  ]);
  assert.match(
    markdown,
    /^\| Scripts \| garde d'une dépendance \| node --test \| `audit` \| indisponible \| indisponible /m,
  );
  assert.match(markdown, /^\| Batterie de simulation \| indisponible \| /m);
  assert.match(markdown, /^\| Scénarios de simulation \| indisponible \| /m);
  assert.equal(line(markdown, "Scripts")[1], "236 ¹");
  assert.equal(report.projects.find(({ id }) => id === "simulation")?.tests, null);
  assert.equal(report.suites.find(({ id }) => id === "convex")?.state, "not_run");

  // Nothing at all, not even how the jobs ended: every line says so, and no number is made up.
  const empty = read(renderReport(buildReport({ parts: {}, junit: {}, needs: undefined, event: "", run: {} })));
  assert.match(empty, /^\*\*0 tests lancés, 0 en échec\.\*\* 0 gates réussies sur 6, 6 dans un état inconnu /m);
  assert.deepEqual(line(empty, "Console du Pi").slice(1), [
    ...Array.from({ length: 9 }, () => "non lancé"),
    "❔ état inconnu : `pi-gate`",
  ]);
});

test("the audit and the documentation appear by how they ended, and by nothing they found", () => {
  const report = buildReport({
    parts: everyPart(),
    junit: everyScript(),
    needs: needs({ audit: "failure" }),
    event: "push",
    run: RUN,
  });
  const markdown = read(renderReport(report));
  assert.match(
    markdown,
    /^Autres gates : `audit` ❌ échec · `docs` ✅ réussie\. Ce qu'elles contrôlent se lit dans leur journal, pas ici\.$/m,
  );
  assert.equal(line(markdown, "Scripts")[10], "❌ échec : `audit`");
  // Of the audit job the report knows two test files and a result: no suite, no measure, no check reads anything else.
  assert.deepEqual(
    SUITES.filter(({ job }) => job === "audit").map(({ id, runner }) => `${id} ${runner}`),
    ["scripts-dependency-guard node --test", "scripts-gitleaks-fixture node --test"],
  );
  assert.ok(COVERAGES.every(({ job }) => job !== "audit" && job !== "docs"));
  assert.ok(Object.values(CHECK_JOBS).every(({ lint, types }) => ![...lint, ...types].includes("audit")));
  assert.doesNotMatch(JSON.stringify(report) + markdown, /vulnerab|alerte|CodeQL|secret/i);
});

test("the catalogue of the report names each suite and each measure once, under a project and a gate that exist", () => {
  const ids = [...SUITES, ...COVERAGES].map(({ id, part }) => `${part}/${id}`);
  assert.equal(new Set(SUITES.map(({ id }) => id)).size, SUITES.length);
  assert.equal(new Set(COVERAGES.map(({ id }) => id)).size, COVERAGES.length);
  assert.ok(ids.length > 0);
  const projects = PROJECTS.map(({ id }) => id);
  for (const { id, project, job } of [...SUITES, ...COVERAGES]) {
    assert.ok(projects.includes(project), `${id}: project ${project}`);
    assert.ok(["changes", ...GATES].includes(job), `${id}: job ${job}`);
  }
  assert.deepEqual(Object.keys(CHECK_JOBS), projects);
  // One measure per project is the one the table shows, except for the scripts, which have none.
  assert.deepEqual(
    projects.map((project) => COVERAGES.filter((measure) => measure.project === project && measure.main).length),
    [1, 1, 1, 1, 0],
  );
  assert.deepEqual(
    PROJECTS.flatMap(({ gates }) => gates).filter((job) => GATES.includes(job)),
    GATES,
    "each required check is shown on one line",
  );
});

test("the essentials of the run are also said in plain text, one line per project, for a notice", () => {
  const all = buildReport({ parts: everyPart(), junit: everyScript(), needs: needs(), event: "push", run: RUN });
  assert.deepEqual(read(renderNotice(all)).split("\n"), [
    "5 583 tests lancés, 0 en échec. 6 gates réussies sur 6.",
    "Console du Pi : 3 248 tests, 0 en échec ; lignes 91,2 %, branches 88,0 % ; lint réussi ; types réussis ; gate réussie",
    "Simulation : 1 024 tests, 0 en échec ; lignes 100 %, branches 100 % ; lint réussi ; types réussis ; gate réussie",
    "Convex : 925 tests, 0 en échec ; lignes 96,6 %, branches 87,3 % ; lint réussi ; types réussis ; gate réussie",
    "Site : 136 tests, 0 en échec ; lignes 15,9 %, branches 20,4 % ; lint réussi ; types réussis ; gate réussie",
    "Scripts : 250 tests, 0 en échec ; couverture non mesurée ; lint réussi ; types réussis ; gate réussie",
    "Le tableau complet est le résumé du job quality-report, dernier bloc de cette page.",
  ]);
  // A site-only pull request whose `web` gate failed on a lint error, with no artifact from `convex-tests`.
  const { site } = everyPart();
  site.checks[1] = check("lint", "site", "web", "failed");
  const states = needs(
    { "pi-gate": "skipped", "simulation-gate": "skipped", web: "failure" },
    { python: "false", node: "true" },
  );
  const partly = buildReport({ parts: { site }, junit: everyScript(), needs: states, event: "pull_request", run: RUN });
  assert.deepEqual(read(renderNotice(partly)).split("\n").slice(0, 6), [
    "324 tests lancés, 0 en échec. 3 gates réussies sur 6 (pi-gate sautée, simulation-gate sautée, web en échec).",
    "Console du Pi : sautée par la règle de chemins",
    "Simulation : sautée par la règle de chemins",
    "Convex : tests indisponibles ; couverture indisponible ; lint réussi ; types indisponibles ; gate réussie",
    "Site : 136 tests, 0 en échec ; lignes 15,9 %, branches 20,4 % ; lint en échec ; types réussis ; gate en échec",
    "Scripts : 148 tests, 0 en échec ; couverture non mesurée ; lint réussi (partiel) ; types sautés ; gate réussie",
  ]);
  // What the runner reads: one line, the line breaks and the characters of its own syntax escaped.
  assert.equal(
    notice("Rapport, 100 % : fait", "a 50 %\nb\r\n::error::c"),
    "::notice title=Rapport%2C 100 %25 %3A fait::a 50 %25%0Ab%0D%0A::error::c",
  );
});

// --- The commands ---------------------------------------------------------------------------

test("the two commands write the summaries and quality-report.json from what the jobs left", (context) => {
  const directory = scratch(context);
  const artifacts = join(directory, "artifacts");
  const summary = join(directory, "summary.md");
  const env = {
    GITHUB_STEP_SUMMARY: summary,
    GITHUB_WORKSPACE: ROOT,
    GITHUB_RUN_ID: "37614000000",
    GITHUB_RUN_ATTEMPT: "2",
    GITHUB_EVENT_NAME: "pull_request",
    GITHUB_REPOSITORY: "anheartpro-byte/Anheart",
    GITHUB_SERVER_URL: "https://github.com",
    GITHUB_SHA: "ffffffffffffffff",
    HEAD_SHA: "0123456789abcdef",
    PR_NUMBER: "40",
  };
  // In `pi-gate`: the report directory of the gate becomes the artifact `quality-pi`.
  piGateLeft(join(artifacts, "quality-pi"));
  assert.equal(main(["job", "pi", "--dir", join(artifacts, "quality-pi"), "--shares", "2"], env), 0);
  assert.equal(JSON.parse(readFileSync(join(artifacts, "quality-pi", "part.json"), "utf8")).part, "pi");
  assert.match(readFileSync(summary, "utf8"), /Détail de la qualité : console du Pi/);
  // In `changes`, `docs`: the JUnit file as `node --test` wrote it.
  write(artifacts, "quality-scripts-changes/scripts-ci.xml", NODE);
  write(artifacts, "quality-scripts-docs/scripts-men.xml", NODE.replace(/<failure[\s\S]*?<\/failure>/, ""));
  // An artifact of a later format is not read as if it were this one.
  write(
    artifacts,
    "quality-convex/part.json",
    JSON.stringify({ schema: SCHEMA_VERSION + 1, part: "convex", suites: { convex: numbers(1) } }),
  );

  const out = join(directory, "out");
  const skipped = { "simulation-gate": "skipped", "pi-gate": "failure" };
  const NEEDS = JSON.stringify(needs(skipped, { python: "true", node: "true" }));
  assert.equal(main(["report", "--artifacts", artifacts, "--out", out], { ...env, NEEDS }), 0);

  const report = JSON.parse(readFileSync(join(out, "quality-report.json"), "utf8"));
  assert.equal(report.schema, SCHEMA_VERSION);
  assert.deepEqual(report.run, {
    id: "37614000000",
    attempt: 2,
    event: "pull_request",
    repository: "anheartpro-byte/Anheart",
    ref: null,
    sha: "0123456789abcdef",
    pull_request: 40,
    url: "https://github.com/anheartpro-byte/Anheart/actions/runs/37614000000",
  });
  assert.deepEqual(Object.keys(report), ["schema", "run", "jobs", "projects", "suites", "coverage", "scenarios"]);
  assert.deepEqual(report.jobs["pi-gate"], "failed");
  assert.deepEqual(
    report.projects.map((/** @type {{id: string}} */ project) => project.id),
    PROJECTS.map(({ id }) => id),
  );
  const pi = report.projects[0];
  assert.deepEqual(pi.tests, { total: 7, passed: 4, failed: 1, skipped: 2, duration_s: 14.25, complete: false });
  assert.deepEqual(pi.coverage, { lines: { covered: 325, total: 400 }, branches: { covered: 124, total: 140 } });
  assert.deepEqual([pi.lint.state, pi.types.state, pi.gate.state], ["passed", "failed", "failed"]);
  const scripts = report.projects[4];
  assert.deepEqual(
    [scripts.tests.total, scripts.tests.failed, scripts.coverage, scripts.coverage_state],
    [8, 1, null, "not_measured"],
  );
  assert.equal(report.suites.find((/** @type {{id: string}} */ suite) => suite.id === "convex").state, "unavailable");
  assert.equal(
    report.suites.find((/** @type {{id: string}} */ suite) => suite.id === "simulation-battery").state,
    "not_run",
  );
  // The same table in the summary of the run and next to the JSON.
  const markdown = readFileSync(join(out, "quality-report.md"), "utf8");
  assert.ok(readFileSync(summary, "utf8").endsWith(markdown));
  // On GitHub the essentials are also printed as one notice, on one line; elsewhere nothing is printed.
  const printed = context.mock.method(process.stdout, "write", () => true);
  assert.equal(main(["report", "--artifacts", artifacts, "--out", out], { ...env, NEEDS }), 0);
  assert.equal(printed.mock.callCount(), 0);
  assert.equal(main(["report", "--artifacts", artifacts, "--out", out], { ...env, NEEDS, GITHUB_ACTIONS: "true" }), 0);
  const [line, ...more] = printed.mock.calls.map((call) => String(call.arguments[0]));
  printed.mock.restore();
  assert.deepEqual(more, []);
  assert.match(
    line ?? "",
    /^::notice title=Rapport de qualité::15 tests lancés, 2 en échec\. 4 gates réussies sur 6 \(/,
  );
  assert.match(
    line ?? "",
    /%0AConsole du Pi : 7 tests, 1 en échec ; lignes 81,2 %25, branches 88,5 %25 ; lint réussi ; types en échec ; gate en échec%0A/,
  );
  assert.equal((line ?? "").trimEnd().split("\n").length, 1, "a notice is one line of output");
  assert.match(
    markdown,
    /^\| Console du Pi \| 7 ¹ \| 4 \| \*\*1\*\* \| 2 \| 14,3 s \| 81,2 % \| 88,5 % \| ✅ réussi \| ❌ échec \| ❌ échec : `pi-gate` \|$/m,
  );
  assert.doesNotMatch(readFileSync(summary, "utf8") + JSON.stringify(report), /SECRET|ERR_TEST_FAILURE/);
});

test("the report never fails the job that calls it: it exits 0 and says « rapport indisponible »", (context) => {
  const directory = scratch(context);
  const summary = join(directory, "summary.md");
  const env = { GITHUB_STEP_SUMMARY: summary };
  const said = () => {
    const text = existsSync(summary) ? readFileSync(summary, "utf8") : "";
    rmSync(summary, { force: true });
    return text;
  };
  const quiet = context.mock.method(console, "error", () => {});
  for (const argv of [
    [],
    ["publish"],
    ["report"],
    ["report", "--artifacts", directory],
    ["report", "--out"],
    // The place to write is under a file.
    ["report", "--artifacts", directory, "--out", join(write(directory, "a file", ""), "below")],
  ]) {
    assert.equal(main(argv, env), 0, argv.join(" "));
    assert.match(said(), /^\*\*Rapport de qualité : rapport indisponible\.\*\* /, argv.join(" "));
  }
  for (const argv of [
    ["job"],
    ["job", "pi"],
    ["job", "nothing", "--dir", directory],
    ["job", "pi", "--dir", directory, "--shares", "four"],
  ]) {
    assert.equal(main(argv, env), 0, argv.join(" "));
    assert.match(said(), /^\*\*Détail de la qualité \(.+\) : rapport indisponible\.\*\* /, argv.join(" "));
  }
  assert.equal(quiet.mock.callCount(), 10);
  // Nowhere to say it either: still 0.
  assert.equal(main(["report"], { GITHUB_STEP_SUMMARY: join(directory, "a file", "below") }), 0);
  // An empty directory of artifacts and no state of the jobs is a report, not a failure.
  assert.equal(
    main(["report", "--artifacts", join(directory, "none"), "--out", join(directory, "out")], { ...env, NEEDS: "{" }),
    0,
  );
  assert.match(
    said(),
    /^## Rapport de qualité\n\n\*\*0 tests lancés, 0 en échec\.\*\* 0 gates réussies sur 6, 6 dans un état inconnu/,
  );
  // Started as a program, it ends the same way.
  const started = spawnSync(process.execPath, [join(ROOT, "scripts/ci/quality-report.mjs"), "nothing"], {
    encoding: "utf8",
    env: {},
  });
  assert.equal(started.status, 0);
  assert.match(started.stdout, /rapport indisponible/);
});

// --- The documentation -------------------------------------------------------------------------

test("the documentation describes every column of the table and the format of quality-report.json", () => {
  const docs = readFileSync(join(ROOT, "docs/framework-de-test.md"), "utf8");
  const section = /^### Rapport de qualité.*\n[\s\S]*?(?=^### )/m.exec(docs)?.[0] ?? "";
  assert.ok(section.length > 0, "docs/framework-de-test.md has no section on the quality report");
  for (const column of HEADER.slice(1))
    assert.ok(section.includes(`| ${column} |`), `column "${column}" is not described`);
  for (const { label } of PROJECTS) assert.ok(section.includes(label.replace(/ \(.*$/, "")), `project "${label}"`);
  for (const word of ["sautée", "indisponible", "non lancé", "non mesurée", "rapport indisponible"]) {
    assert.ok(section.includes(word), `"${word}" is not explained`);
  }
  assert.ok(
    section.includes(`\`schema\` | \`${SCHEMA_VERSION}\``),
    "the documented version of the format is not this one",
  );
  for (const key of ["run", "jobs", "projects", "suites", "coverage", "scenarios"]) {
    assert.ok(section.includes(`\`${key}\``), `the field "${key}" of quality-report.json is not described`);
  }
});
