// What must stay true of .github/workflows/ci.yml for the path rule to be safe.
//
// Run by the `changes` job before it decides anything, without any install:
// the workflow is read as text, job by job. These are the properties a later
// edit of the workflow could break without any gate turning red.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { CONVEX, SITE, siteFolders, sourcesOf, testsOf, vitestThresholds } from "./coverage-thresholds.mjs";
import { COVERAGES, GATES, SUITES } from "./quality-report.mjs";

const workflow = readFileSync(new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8");

/** Each job's own lines, by job id. @type {Map<string, string>} */
const jobs = new Map();
{
  const body = workflow.slice(workflow.indexOf("\njobs:\n") + "\njobs:\n".length);
  const starts = [...body.matchAll(/^ {2}([a-z][a-z0-9-]*):[ \t]*$/gm)];
  starts.forEach((start, index) => {
    const end = starts[index + 1]?.index ?? body.length;
    jobs.set(start[1] ?? "", body.slice(start.index, end));
  });
}

/** @param {string} id @param {string} key a key of the job itself, not of one of its steps */
const own = (id, key) => new RegExp(`^ {4}${key}:[ \\t]*(.*)$`, "m").exec(jobs.get(id) ?? "")?.[1];

/**
 * The shell text of one step of a job, as the runner would be handed it.
 * @param {string} id @param {string} name the step's `name:`
 */
const script = (id, name) => {
  const step = (jobs.get(id) ?? "").split(/^ {6}- /m).find((text) => text.startsWith(`name: ${name}\n`));
  const block = /^ {8}run: \|\n((?: {10}.*\n|\n)+)/m.exec(step ?? "")?.[1];
  assert.ok(block, `${id}: no step "${name}" with a run block`);
  return block.replaceAll(/^ {10}/gm, "");
};

/** @param {string} id @param {string} name the step's `name:` @returns {string | undefined} its `if:` */
const stepCondition = (id, name) => {
  const step = (jobs.get(id) ?? "").split(/^ {6}- /m).find((text) => text.startsWith(`name: ${name}\n`));
  return /^ {8}if:[ \t]*(.*)$/m.exec(step ?? "")?.[1];
};

const REQUIRED = ["pi-gate", "simulation-gate", "convex-tests", "web", "audit", "docs"];
const ALWAYS = ["changes", "audit", "docs"];
/** The job that writes the quality report of the run: it waits for the gates and decides none. */
const REPORT = "quality-report";
const condition = (/** @type {string} */ output, started = "!cancelled()") =>
  `\${{ ${started} && (github.event_name != 'pull_request' || needs.changes.outputs.${output} != 'false') }}`;
/** Each job the path rule may skip, with the exact condition it must carry. */
const GATED = new Map([
  ["pi-gate", condition("python")],
  ["simulation-battery", condition("python")],
  ["simulation-report", condition("python")],
  // The job that waits for the others: see the test of a cancelled run below.
  ["simulation-gate", condition("python", "always()")],
  ["convex-tests", condition("node")],
  ["web", condition("node")],
]);

test("the jobs are the ones this file knows about", () => {
  assert.deepEqual([...jobs.keys()].sort(), [...ALWAYS, ...GATED.keys(), REPORT].sort());
});

test("every required check is the name of one plain job", () => {
  for (const id of REQUIRED) {
    assert.ok(jobs.has(id), `${id}: no such job`);
    assert.equal(own(id, "name"), undefined, `${id}: a display name would rename the required check`);
    assert.equal(own(id, "strategy"), undefined, `${id}: a matrix job reports under other names`);
  }
  for (const [id] of jobs) {
    const shown = own(id, "name");
    assert.ok(shown === undefined || !REQUIRED.includes(shown), `${id} is shown as ${shown}`);
  }
});

test("a gate can only be skipped on a pull request, and only by an explicit answer", () => {
  for (const [id, expected] of GATED) {
    assert.equal(own(id, "if"), expected, id);
    assert.match(own(id, "needs") ?? "", /^(changes|\[changes(, [a-z-]+)*\])$/, id);
  }
  // The rule is read nowhere else: no step, no other expression can skip a gate.
  const reads = workflow.split("needs.changes.outputs").length - 1;
  assert.equal(reads, GATED.size);
});

test("documentation, audit and the rule itself run on every event", () => {
  for (const id of ALWAYS) {
    assert.equal(own(id, "if"), undefined, id);
    assert.equal(own(id, "needs"), undefined, id);
  }
  const triggers = workflow.slice(0, workflow.indexOf("\npermissions:"));
  assert.match(triggers, /^ {2}push:\n {4}branches: \[main, develop\]$/m);
  assert.match(triggers, /^ {2}schedule:\n {4}- cron: /m);
  assert.doesNotMatch(triggers, /paths(-ignore)?:/, "a filtered workflow leaves its required checks pending");
});

test("the Convex functions are type-checked before their tests", () => {
  const steps = jobs.get("convex-tests") ?? "";
  const typecheck = steps.indexOf("run: npx tsc -p convex/tsconfig.json --noEmit\n");
  assert.ok(typecheck > steps.indexOf("run: npm ci\n"), "tsc needs the dependencies");
  assert.ok(typecheck > 0 && typecheck < steps.indexOf("run: npm run test:convex -- "));
});

test("the parts of the simulation battery name every share exactly once", () => {
  const count = Number(/^ {2}SIMULATION_SHARES: '(\d+)'$/m.exec(workflow)?.[1]);
  assert.ok(Number.isInteger(count) && count > 0);
  const spans = [...(jobs.get("simulation-battery") ?? "").matchAll(/^ {12}shares: (\d+)-(\d+)$/gm)];
  const named = spans.flatMap(([, first, last]) => {
    const shares = [];
    for (let share = Number(first); share <= Number(last); share += 1) shares.push(share);
    return shares;
  });
  assert.deepEqual(
    named.sort((a, b) => a - b),
    Array.from({ length: count }, (_, share) => share),
  );
});

const PARTS = "Every part of the gate succeeded";

/** How the first step of `simulation-gate` ends, given how the jobs it waited for ended. */
const partsVerdict = (/** @type {string} */ battery, /** @type {string} */ report) =>
  spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script("simulation-gate", PARTS)], {
    env: { PATH: process.env.PATH ?? "", BATTERY: battery, REPORT: report },
    encoding: "utf8",
  }).status;

test("the simulation gate asks how every part ended before it judges their records", () => {
  const gate = jobs.get("simulation-gate") ?? "";
  assert.equal(own("simulation-gate", "needs"), "[changes, simulation-battery, simulation-report]");
  assert.match(gate, /BATTERY: \$\{\{ needs\.simulation-battery\.result \}\}/);
  assert.match(gate, /REPORT: \$\{\{ needs\.simulation-report\.result \}\}/);
  assert.match(gate, /SIMULATION_GATE_COMBINE: \$\{\{ env\.SIMULATION_SHARES \}\}/);
  assert.ok(gate.indexOf(`- name: ${PARTS}\n`) < gate.indexOf("- uses: "), "it must be the first step");
  assert.equal(partsVerdict("success", "success"), 0);
});

test("a cancelled run leaves the simulation gate failed, never skipped", () => {
  // A job whose condition is false is skipped, and a skipped job passes a
  // required check. `!cancelled()` is false once the run is cancelled, so the
  // job that waits for the battery must not carry it: it starts, and its
  // first step, which runs whatever happened, fails on what the parts report.
  assert.match(own("simulation-gate", "if") ?? "", /^\$\{\{ always\(\) && /);
  assert.equal(stepCondition("simulation-gate", PARTS), "${{ always() }}");
  // What GitHub gives as `needs.<job>.result`: a part that ran out of time is a failure.
  for (const other of ["cancelled", "failure", "skipped", ""]) {
    assert.notEqual(partsVerdict(other, "success"), 0, `battery ${other || "unknown"}`);
    assert.notEqual(partsVerdict("success", other), 0, `report ${other || "unknown"}`);
    assert.notEqual(partsVerdict(other, other), 0, `both ${other || "unknown"}`);
  }
  // No other step of that job may run on a cancelled run, nor be what decides.
  const later = (jobs.get("simulation-gate") ?? "").split(/^ {6}- /m).slice(2);
  assert.ok(later.length >= 5);
  for (const step of later) assert.match(step, /^ {8}if: \$\{\{ !cancelled\(\) \}\}$/m, step.split("\n")[0]);
});

// --- The quality report (ANH-199; docs/framework-de-test.md, "Rapport de qualité") ---
//
// It reads what the gates leave and decides none of them. What follows holds
// that: a later edit could let a step of the report fail a required check, or
// hand the public summary something it must not carry, without any job
// turning red.

/** The steps of a job, each as its own lines. @param {string} id */
const stepsOf = (id) => (jobs.get(id) ?? "").split(/^ {6}- /m).slice(1);
/** The first line of a step, for a message. @param {string} step */
const titleOf = (step) => step.split("\n")[0];
/** A step the quality report adds to a job that is not its own. @param {string} step */
const addedForTheReport = (step) =>
  /^name: (Quality report, the numbers of this job|Keep the numbers for the quality report)/.test(step);

test("the report job waits for every gate, runs whatever happened to them, and nothing waits for it", () => {
  assert.deepEqual(GATES, REQUIRED, "the report shows the checks branch protection requires");
  assert.equal(own(REPORT, "needs"), `[changes, ${REQUIRED.join(", ")}]`);
  // After a gate that failed, was skipped or was cancelled there is still a table to write.
  assert.equal(own(REPORT, "if"), "${{ always() }}");
  assert.ok(!REQUIRED.includes(REPORT));
  assert.equal(own(REPORT, "name"), undefined);
  assert.equal(own(REPORT, "strategy"), undefined);
  for (const [id] of jobs) assert.doesNotMatch(own(id, "needs") ?? "", /quality-report/, `${id} waits for the report`);
  // How each gate ended, the answer of the path rule included, comes from `needs` as one value.
  assert.match(jobs.get(REPORT) ?? "", /^ {10}NEEDS: \$\{\{ toJSON\(needs\) \}\}$/m);
  assert.match(jobs.get(REPORT) ?? "", /^ {10}pattern: quality-\*$/m);
  // The table is the last block of the page of the run: the job hands the report what GitHub
  // anchors its own summary at, so that the notice can end on the address of the table.
  assert.match(jobs.get(REPORT) ?? "", /^ {10}CHECK_RUN_ID: \$\{\{ job\.check_run_id \}\}$/m);
});

test("the link checker keeps its report in its log, and writes no block above the table of the run", () => {
  const checker = stepsOf("docs").find((step) => step.includes("lycheeverse/lychee-action@")) ?? "";
  assert.match(checker, /^ {10}jobSummary: false$/m);
  // What it checks, and what makes the gate fail, are as they were.
  assert.match(checker, /^ {10}args: --offline --include-fragments --no-progress 'README\.md' 'docs\/\*\*\/\*\.md'$/m);
  assert.doesNotMatch(checker, /^ {10}fail(IfEmpty)?: false$|continue-on-error/m);
});

test("no step of the report job can fail it, and it says so when it wrote no report", (context) => {
  const steps = stepsOf(REPORT);
  assert.ok(steps.length >= 5, "the steps of the report job were not read");
  for (const step of steps) assert.match(step, /^ {8}continue-on-error: true$/m, titleOf(step));
  // The step that speaks when the script could not: run as written, with and without a report.
  const SAY = "Say so when no report was written";
  assert.equal(stepCondition(REPORT, SAY), "${{ always() }}");
  const directory = mkdtempSync(join(tmpdir(), "anheart-report-job-"));
  context.after(() => rmSync(directory, { recursive: true, force: true }));
  const summary = join(directory, "summary.md");
  const say = () => {
    writeFileSync(summary, "");
    const ran = spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script(REPORT, SAY)], {
      env: { PATH: process.env.PATH ?? "", RUNNER_TEMP: directory, GITHUB_STEP_SUMMARY: summary },
      encoding: "utf8",
    });
    assert.equal(ran.status, 0, ran.stderr);
    return readFileSync(summary, "utf8");
  };
  assert.match(say(), /^\*\*Rapport de qualité : rapport indisponible\.\*\*/);
  writeFileSync(join(directory, "quality-report.json"), "{}");
  assert.match(say(), /rapport indisponible/, "a file elsewhere is not the report");
  spawnSync("mkdir", ["-p", join(directory, "quality-report")]);
  writeFileSync(join(directory, "quality-report", "quality-report.json"), "");
  assert.match(say(), /rapport indisponible/, "an empty file is not a report");
  writeFileSync(join(directory, "quality-report", "quality-report.json"), '{"schema":1}\n');
  assert.equal(say(), "");
});

test("what the report adds to a gate cannot change the verdict of that gate", () => {
  let added = 0;
  for (const [id] of jobs) {
    if (id === REPORT) continue;
    for (const step of stepsOf(id)) {
      const forTheReport = addedForTheReport(step);
      // Its script and its artifacts are only reached from the steps it added.
      const reaches = /quality-report\.mjs|^ {10}name: quality-/m.test(step);
      assert.equal(reaches, forTheReport, `${id}: ${titleOf(step)}`);
      if (!forTheReport) continue;
      added += 1;
      assert.match(step, /^ {8}continue-on-error: true$/m, `${id}: ${titleOf(step)}`);
      assert.match(step, /^ {8}if: \$\{\{ !cancelled\(\) \}\}$/m, `${id}: ${titleOf(step)}`);
      // A file that is not there is not an error either.
      if (step.includes("actions/upload-artifact@")) {
        assert.match(step, /^ {10}if-no-files-found: ignore$/m, `${id}: ${titleOf(step)}`);
        assert.match(step, /^ {10}overwrite: true$/m, `${id}: ${titleOf(step)}`);
        assert.match(step, /^ {10}path: \$\{\{ runner\.temp \}\}\/quality$/m, `${id}: ${titleOf(step)}`);
      }
    }
  }
  assert.ok(added >= 11, `${added} steps of the report were read`);
});

// --- The thresholds of Convex and of the site (ANH-203; docs/framework-de-test.md,
// "Seuils de couverture de Convex et du site") ---
//
// Unlike the report, they decide: `convex-tests` and `web` fail under 80 % of
// lines or of branches. What follows holds the chain from the one place the
// threshold is written to the step that fails the job. A later edit could cut
// it anywhere (a `continue-on-error`, a configuration that stops reading the
// list, a script without its flag) and leave every gate green.

/** A file at the root of the repository, as text. @param {string} name */
const atRoot = (name) => readFileSync(new URL(`../../${name}`, import.meta.url), "utf8");

/** The steps that measure the coverage of a job and fail it under the threshold. */
const ENFORCED = [
  {
    job: "convex-tests",
    step: "Enforce the coverage of the Convex functions",
    script: "coverage:convex",
    config: "vitest.convex.config.mts",
    measure: "CONVEX",
    left: "coverage-convex",
  },
  {
    job: "web",
    step: "Enforce the coverage of the site",
    script: "coverage:site",
    config: "vitest.site-coverage.config.mts",
    measure: "SITE",
    left: "coverage-site",
  },
];

test("the coverage of Convex and of the site decides their gates: under the threshold the job fails", () => {
  const scripts = JSON.parse(atRoot("package.json")).scripts;
  for (const { job, step: name, script, config, measure, left } of ENFORCED) {
    const step = stepsOf(job).find((text) => text.startsWith(`name: ${name}\n`)) ?? "";
    assert.ok(step !== "", `${job}: no step "${name}"`);
    // Nothing lets the job pass when the command fails.
    assert.doesNotMatch(step, /continue-on-error/, `${job}: ${name}`);
    // It runs after a failed test too, so that the report keeps its measure; never on a cancelled run.
    assert.equal(stepCondition(job, name), "${{ !cancelled() }}", `${job}: ${name}`);
    // The command of the package, and the measure left where the report reads it.
    assert.match(
      step,
      new RegExp(
        `^ {8}run: npm run ${script} -- --reporter=default --coverage\\.reportsDirectory="\\$RUNNER_TEMP/quality/${left}"$`,
        "m",
      ),
      `${job}: ${name}`,
    );
    // No flag on that line takes the threshold away or changes what is measured.
    assert.doesNotMatch(step, /thresholds|coverage\.(include|exclude|enabled)/, `${job}: ${name}`);
    assert.equal(scripts[script], `vitest run --config ${config} --coverage`);
    // The configuration takes what it measures and its threshold from the one list, and from nowhere else.
    const text = atRoot(config);
    assert.match(text, new RegExp(`^ {6}thresholds: vitestThresholds\\(${measure}\\),$`, "m"), config);
    assert.equal(text.match(/\bthresholds\s*:/g)?.length, 1, `${config} sets a threshold of its own`);
    assert.match(text, /from "\.\/scripts\/ci\/coverage-thresholds\.mjs";$/m, config);
    assert.match(text, /^ {6}provider: "v8",$/m, config);
    // A run with failing tests still leaves its measure.
    assert.match(text, /^ {6}reportOnFailure: true,$/m, config);
    // Never switched on by the file itself: a plain test run measures nothing.
    assert.doesNotMatch(text, /enabled:|autoUpdate|perFile/, config);
  }
  // What each configuration measures is the list, not a copy of it.
  const convex = atRoot("vitest.convex.config.mts");
  assert.match(convex, /^ {6}include: CONVEX\.include,\n {6}exclude: CONVEX\.exclude,$/m);
  const site = atRoot("vitest.site-coverage.config.mts");
  assert.match(site, /^ {4}include: testsOf\(siteFolders\(\)\),$/m, "the measured run runs every test of the site");
  assert.match(site, /^const measure = \{ include: sourcesOf\(siteFolders\(\)\), exclude: SITE\.exclude \};$/m);
  assert.match(site, /^ {6}include: measure\.include,\n {6}exclude: measure\.exclude,$/m);
  // Each command that enforces a threshold refuses to start when what the threshold names is not
  // measured: Vitest would count the threshold of a name that matches no file as reached. The check is
  // made by the configuration itself, on the lists it measures with, before the configuration is given.
  for (const [text, call] of /** @type {const} */ ([
    [convex, "assertMeasured(import.meta.dirname, CONVEX);"],
    [site, "assertMeasured(import.meta.dirname, measure);"],
  ])) {
    const at = text.indexOf(`\n${call}\n`);
    assert.ok(at > 0, `${call} is not called`);
    assert.ok(at < text.indexOf("\nexport default defineConfig("), `${call} must come before the configuration`);
    assert.doesNotMatch(
      text,
      /try \{|catch|\/\/ *assertMeasured|if \(.*\) assertMeasured/,
      "the check must not be softened",
    );
  }
  // The measured run of the site is the two plain suites together: each takes its folders from the same list.
  assert.match(atRoot("vitest.ecg.config.mts"), /^ {4}include: testsOf\(SITE\.suites\.ecg\),$/m);
  assert.match(atRoot("vitest.site.config.mts"), /^ {4}include: testsOf\(SITE\.suites\.site\),$/m);
  assert.deepEqual(siteFolders(), [...SITE.suites.ecg, ...SITE.suites.site]);
  assert.deepEqual(testsOf(siteFolders()).length, sourcesOf(siteFolders()).length);
  // 80 % of lines and of branches, on the whole and on each Convex file of the safety chain taken alone.
  const required = { lines: 80, branches: 80 };
  assert.deepEqual(vitestThresholds(SITE), required);
  assert.deepEqual(vitestThresholds(CONVEX), {
    ...required,
    ...Object.fromEntries(
      ["convex/training.ts", "convex/http.ts", "convex/lib/auth.ts"].map((file) => [file, required]),
    ),
  });
});

test("the commands a developer runs to test measure nothing, in the CI as on a desk", () => {
  const scripts = JSON.parse(atRoot("package.json")).scripts;
  for (const name of ["test:convex", "test:ecg", "test:site"]) {
    assert.doesNotMatch(scripts[name], /coverage/, `npm run ${name} measures the coverage`);
  }
  // The two suites of the site hold no measure of their own: one run measures the site.
  for (const config of ["vitest.ecg.config.mts", "vitest.site.config.mts"]) {
    assert.doesNotMatch(atRoot(config), /^ *(coverage|thresholds):/m, config);
  }
  // The runs that decide the tests of `convex-tests` and `web` carry no coverage flag either.
  for (const id of ["convex-tests", "web"]) {
    const deciding = stepsOf(id).filter((step) => /^run: npm run test:/.test(step));
    assert.ok(deciding.length >= 1, id);
    // The command itself: the comment that follows it in the file belongs to the next step.
    for (const step of deciding) assert.doesNotMatch(titleOf(step) ?? "", /coverage/, `${id}: ${titleOf(step)}`);
  }
  // Coverage is measured in those two steps and nowhere else in the workflow.
  const measuring = [...jobs].flatMap(([id]) =>
    stepsOf(id)
      .filter((step) => /npm run coverage:|--coverage\b/.test(step))
      .map((step) => `${id}: ${titleOf(step)}`),
  );
  assert.deepEqual(
    measuring,
    ENFORCED.map(({ job, step }) => `${job}: name: ${step}`),
  );
});

test("each suite and each measure of the report is left by the job the report expects it from", () => {
  for (const { id, job, part, runner } of SUITES) {
    const lines = jobs.get(job) ?? "";
    assert.ok(lines.includes(`\n          name: quality-${part}\n`), `${job} publishes no quality-${part}`);
    // pytest writes one file per process, where the gate script tells it to (see below).
    if (runner !== "pytest") assert.ok(lines.includes(`"$RUNNER_TEMP/quality/${id}.xml"`), `${job} keeps no ${id}.xml`);
  }
  for (const { id, job, part } of COVERAGES) {
    assert.ok(
      (jobs.get(job) ?? "").includes(`\n          name: quality-${part}\n`),
      `${id}: ${job} publishes no quality-${part}`,
    );
  }
  // The gate scripts are told where to leave what the report reads: next to the
  // evidence of each job of the simulation battery, so that it reaches the gate with it.
  assert.match(jobs.get("pi-gate") ?? "", /^ {10}QUALITY_REPORT_DIR: \$\{\{ runner\.temp \}\}\/quality$/m);
  assert.match(jobs.get("simulation-gate") ?? "", /^ {10}QUALITY_REPORT_DIR: \$\{\{ runner\.temp \}\}\/quality$/m);
  assert.match(
    jobs.get("simulation-battery") ?? "",
    /^ {10}QUALITY_REPORT_DIR: \$\{\{ runner\.temp \}\}\/simulation-evidence\/quality-\$\{\{ matrix\.shares \}\}$/m,
  );
  assert.match(jobs.get("simulation-gate") ?? "", / --parts "\$RUNNER_TEMP\/simulation-evidence" /);
  // As many JUnit files as processes: the count each job gives is the one its gate ran with.
  assert.match(
    script("pi-gate", "Pi gate"),
    /^PI_GATE_PROCESSES="\$\(nproc\)" bash raspberry-pi\/scripts\/check\.sh$/m,
  );
  assert.match(
    jobs.get("pi-gate") ?? "",
    /quality-report\.mjs job pi --dir "\$RUNNER_TEMP\/quality" --shares "\$\(nproc\)"$/m,
  );
  assert.match(jobs.get("simulation-gate") ?? "", / --shares "\$SIMULATION_SHARES"$/m);
  // The checks of Convex and of the site are steps: their outcome is handed over as GitHub gives it.
  assert.match(jobs.get("convex-tests") ?? "", /^ {8}id: types\n {8}run: npx tsc -p convex\/tsconfig\.json --noEmit$/m);
  assert.match(jobs.get("convex-tests") ?? "", /^ {10}TYPES_OUTCOME: \$\{\{ steps\.types\.outcome \}\}$/m);
  assert.match(jobs.get("web") ?? "", /^ {6}- id: types\n {8}run: npx tsc --noEmit$/m);
  assert.match(jobs.get("web") ?? "", /^ {6}- id: lint\n {8}run: npm run lint$/m);
  assert.match(
    jobs.get("web") ?? "",
    /^ {10}TYPES_OUTCOME: \$\{\{ steps\.types\.outcome \}\}\n {10}LINT_OUTCOME: \$\{\{ steps\.lint\.outcome \}\}$/m,
  );
});

test("the audit hands the report the JUnit files of its two test files, and nothing of what it found", () => {
  const kept = (/** @type {string} */ text) =>
    [...text.matchAll(/\$RUNNER_TEMP\/quality\/([\w.-]+)/g)].map(([, name]) => name).sort();
  assert.deepEqual(kept(jobs.get("audit") ?? ""), ["scripts-dependency-guard.xml", "scripts-gitleaks-fixture.xml"]);
  assert.deepEqual(kept(jobs.get("docs") ?? ""), ["scripts-men.xml"]);
  assert.deepEqual(kept(jobs.get("changes") ?? ""), ["scripts-ci.xml"]);
  // Each of those files is written by `node --test` itself, for the test file named on the same line.
  for (const [id, file] of [
    ["audit", "scripts/ci/braces-depth-guard.test.mjs"],
    ["audit", "scripts/ci/gitleaks-fixture.test.mjs"],
    ["docs", "scripts/ci/check-men.test.mjs"],
  ]) {
    const line = (jobs.get(id ?? "") ?? "").split("\n").find((text) => text.trimEnd().endsWith(` ${file}`)) ?? "";
    assert.match(
      line,
      / node --test \$NODE_TEST_REPORT --test-reporter-destination="\$RUNNER_TEMP\/quality\/[\w-]+\.xml" /,
      file,
    );
  }
  // The audit still runs the test file `npm run test:dependency-security` names.
  const scripts = JSON.parse(readFileSync(new URL("../../package.json", import.meta.url), "utf8")).scripts;
  assert.equal(scripts["test:dependency-security"], "node --test scripts/ci/braces-depth-guard.test.mjs");
  // The static analysis is another workflow: nothing of it reaches the report.
  const codeql = readFileSync(new URL("../../.github/workflows/codeql.yml", import.meta.url), "utf8");
  assert.doesNotMatch(codeql, /quality|upload-artifact|GITHUB_STEP_SUMMARY/);
  assert.doesNotMatch(jobs.get(REPORT) ?? "", /security|code-scanning|audit\b.*\bjson/i);
});

test("the workflow gives no token more than read access, and each action is pinned to one full commit", () => {
  assert.match(workflow, /^permissions:\n {2}contents: read\n\n/m);
  assert.equal(workflow.split(/^ *permissions:/m).length - 1, 1, "a job asks for permissions of its own");
  assert.doesNotMatch(workflow, /pull_request_target|workflow_run|\bsecrets\.|: write\b|write-all/);
  // Values of the event are handed to a script through its environment, never written into it.
  for (const [id] of jobs) {
    for (const step of stepsOf(id)) {
      const run = /^ {8}run: (?:\|\n((?: {10}.*\n|\n)+)|(.*)\n)/m.exec(step);
      assert.doesNotMatch(
        run?.[1] ?? run?.[2] ?? "",
        /\$\{\{ *(github\.event|github\.head_ref|needs)\b/,
        `${id}: ${titleOf(step)}`,
      );
    }
  }
  /** @type {Map<string, string>} */
  const pins = new Map();
  const used = [...workflow.matchAll(/^ +(?:- )?uses:[ \t]*(.*)$/gm)].map(([, written = ""]) => written);
  assert.ok(used.length >= 30, "the actions of the workflow were not read");
  for (const written of used) {
    const [, action = "", commit, version] =
      /^([\w.-]+\/[\w./-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$/.exec(written) ?? [];
    assert.ok(action !== "", `"uses: ${written}" is not <action>@<40 hex> # vX.Y.Z`);
    assert.equal(
      pins.get(action) ?? `${commit} ${version}`,
      `${commit} ${version}`,
      `${action} is pinned to two commits`,
    );
    pins.set(action, `${commit} ${version}`);
  }
});
