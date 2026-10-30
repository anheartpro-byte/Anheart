// What must stay true of .github/workflows/ci.yml for the path rule to be safe.
//
// Run by the `docs` job, a required check that runs on every event, without
// any install: the workflow is read as text, job by job. These are the
// properties a later edit of the workflow could break without any gate
// turning red.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";

import {
  CONVEX,
  SITE,
  globToRegExp,
  siteFolders,
  sourcesOf,
  testsOf,
  vitestThresholds,
} from "./coverage-thresholds.mjs";
import { COVERAGES, GATES, SUITES } from "./quality-report.mjs";
import { concurrencyOf, evaluateExpression, evaluateWorkflowValue, workflowName } from "./workflow-concurrency-test-utils.mjs";

const workflow = readFileSync(new URL("../../.github/workflows/ci.yml", import.meta.url), "utf8");
const releaseWorkflow = readFileSync(new URL("../../.github/workflows/release.yml", import.meta.url), "utf8");

test("S-4 CI concurrency separates the release call and only cancels pull requests", () => {
  const ciName = workflowName(workflow);
  const releaseName = workflowName(releaseWorkflow);
  assert.equal(ciName, "CI");
  assert.equal(releaseName, "Release en production (main)");
  const fields = concurrencyOf(workflow);
  let runId = 0;
  const evaluate = (eventName, ref, { number, sha = "S", workflow: runWorkflow = ciName } = {}) => {
    const context = {
      github: {
        workflow: runWorkflow,
        event_name: eventName,
        ref,
        sha,
        run_id: ++runId,
        event: { pull_request: number === undefined ? undefined : { number } },
      },
    };
    return {
      group: evaluateWorkflowValue(fields.group ?? "", context),
      cancel: evaluateWorkflowValue(fields["cancel-in-progress"] ?? "", context),
    };
  };
  const pushMain = evaluate("push", "refs/heads/main");
  const nightMain = evaluate("schedule", "refs/heads/main");
  const releaseCall = evaluate("workflow_dispatch", "refs/heads/main", { workflow: releaseName });
  const pullRequest42 = evaluate("pull_request", "refs/pull/42/merge", { number: 42 });
  const pullRequest43 = evaluate("pull_request", "refs/pull/43/merge", { number: 43 });
  const pushDevelop = evaluate("push", "refs/heads/develop");

  assert.notEqual(releaseCall.group, pushMain.group);
  assert.notEqual(releaseCall.group, nightMain.group);
  assert.equal(releaseCall.cancel, false);
  assert.equal(pushMain.cancel, false);
  assert.equal(nightMain.cancel, false);
  assert.equal(pullRequest42.cancel, true);
  assert.equal(pullRequest43.cancel, true);
  assert.notEqual(pullRequest42.group, pullRequest43.group);
  assert.notEqual(pushMain.group, pushDevelop.group);
  assert.equal(evaluate("pull_request", "refs/pull/42/merge", { number: 42, sha: "T" }).group, pullRequest42.group);
  assert.equal(evaluate("push", "refs/heads/main", { sha: "T" }).group, pushMain.group);
});

test("S-4 concurrency expressions reject unsupported operands before short-circuit evaluation", () => {
  const context = { github: { workflow: "CI", event_name: "push" } };
  for (const expression of [
    "github.workflow || format(1)",
    "github.workflow || github.ref || format(1)",
    "github.event_name == 'push' || format(1)",
    "github.workflow ||",
  ]) {
    assert.throws(() => evaluateExpression(expression, context), /unsupported expression/, expression);
  }
  assert.equal(evaluateExpression("github.event_name == 'schedule' || github.workflow", context), "CI");
  assert.equal(evaluateExpression("github.event_name == 'push' || github.workflow", context), true);
  assert.equal(evaluateExpression("github.workflow || github.ref", {
    github: { workflow: "CI", get ref() { throw new Error("the second operand was evaluated"); } },
  }), "CI");
});

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
  ["pi-tests", condition("python")],
  // The jobs that wait for others: see the test of a cancelled run below.
  ["pi-gate", condition("python", "always()")],
  ["simulation-battery", condition("python")],
  ["simulation-report", condition("python")],
  ["simulation-gate", condition("python", "always()")],
  ["convex-tests", condition("node")],
  ["web", condition("node")],
]);
/**
 * The browser suite (ANH-83): no gate, and the one job that reads a secret.
 * It starts only in this workflow's own runs (never when the release calls
 * it) and once the repository variable of its Clerk instance is set; on a
 * pull request, only from this repository (not a fork), and the path rule
 * skips it when the pull request changes neither the site nor the Python of
 * the console it runs.
 */
const E2E = "e2e";
const E2E_CONDITION =
  "${{ !cancelled() && github.workflow == 'CI' && vars.E2E_CLERK_PUBLISHABLE_KEY != '' && (github.event_name != 'pull_request' || (github.event.pull_request.head.repo.full_name == github.repository && (needs.changes.outputs.node != 'false' || needs.changes.outputs.python != 'false'))) }}";
/** The suite's two markers (e2e/secret-scan.ts): nothing may be sent, or all of it may. */
const E2E_REPORTER = readFileSync(new URL("../../e2e/secret-scan.ts", import.meta.url), "utf8");
const E2E_DO_NOT_SEND = /export const DO_NOT_SEND = "([a-z-]+)";/.exec(E2E_REPORTER)?.[1];
const E2E_MAY_SEND = /export const MAY_SEND = "([a-z-]+)";/.exec(E2E_REPORTER)?.[1];
/** The one secret of this workflow, as the step of the browser suite reads it. */
const E2E_SECRET = "          CLERK_SECRET_KEY: ${{ secrets.E2E_CLERK_SECRET_KEY }}\n";

test("the jobs are the ones this file knows about", () => {
  assert.deepEqual([...jobs.keys()].sort(), [...ALWAYS, ...GATED.keys(), REPORT, E2E].sort());
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
  // The browser suite, which is no gate, reads it in its own condition only.
  const reads = workflow.replace(jobs.get(E2E) ?? "", "").split("needs.changes.outputs").length - 1;
  assert.equal(reads, GATED.size);
  assert.equal((jobs.get(E2E) ?? "").split("needs.changes.outputs").length - 1, 2);
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

// The committed bindings of the Convex functions (ANH-195). The check has no
// job of its own: a step of `convex-tests` fails that required job. What
// follows holds that nothing softens the step, and that it comes first.
test("the committed bindings of Convex are checked by `convex-tests`, before its type-check", () => {
  const name = "Check the committed bindings of the Convex functions";
  const step = stepsOf("convex-tests").find((text) => text.startsWith(`name: ${name}\n`)) ?? "";
  assert.match(step, /^ {8}run: node scripts\/ci\/convex-generated-api\.mjs$/m, `convex-tests: no step "${name}"`);
  // It checks and never rewrites; nothing lets the job pass when it fails, and no condition skips it.
  assert.doesNotMatch(step, /continue-on-error|--write/);
  assert.equal(stepCondition("convex-tests", name), undefined);
  const steps = jobs.get("convex-tests") ?? "";
  assert.ok(steps.indexOf(`- name: ${name}\n`) > steps.indexOf("run: npm ci\n"));
  assert.ok(steps.indexOf(`- name: ${name}\n`) < steps.indexOf("run: npx tsc -p convex/tsconfig.json --noEmit\n"));
  // The workflow asks the Convex command line for nothing: it has no deployment, and must reach none.
  assert.doesNotMatch(workflow, /\bconvex (dev|deploy|codegen|run)\b|CONVEX_DEPLOY/);
});

/**
 * The two suites cut into shares that several jobs run: the variable of the workflow that says into
 * how many, the matrix job that runs them, the required check that judges them, and what that check
 * waits for besides the path rule, each under the name its first step reads it by.
 */
const SHARED = [
  { count: "PI_SHARES", runs: "pi-tests", gate: "pi-gate", combine: "PI_GATE_COMBINE", waits: { TESTS: "pi-tests" } },
  {
    count: "SIMULATION_SHARES",
    runs: "simulation-battery",
    gate: "simulation-gate",
    combine: "SIMULATION_GATE_COMBINE",
    waits: { BATTERY: "simulation-battery", REPORT: "simulation-report" },
  },
];

test("the parts of each suite cut into shares name every share exactly once", () => {
  for (const { count: variable, runs, combine } of SHARED) {
    const count = Number(new RegExp(`^ {2}${variable}: '(\\d+)'$`, "m").exec(workflow)?.[1]);
    assert.ok(Number.isInteger(count) && count > 0, variable);
    const spans = [...(jobs.get(runs) ?? "").matchAll(/^ {12}shares: (\d+)-(\d+)$/gm)];
    const named = spans.flatMap(([, first, last]) => {
      const shares = [];
      for (let share = Number(first); share <= Number(last); share += 1) shares.push(share);
      return shares;
    });
    assert.deepEqual(
      named.sort((a, b) => a - b),
      Array.from({ length: count }, (_, share) => share),
      runs,
    );
    // Each part is told its own span, out of the count the gate will combine.
    const told = `${combine.replace("COMBINE", "SHARES")}: \${{ matrix.shares }}/\${{ env.${variable} }}`;
    assert.ok((jobs.get(runs) ?? "").includes(`\n          ${told}\n`), `${runs}: ${told}`);
  }
});

const PARTS = "Every part of the gate succeeded";

/**
 * How the first step of a gate that judges shares ends, given how the jobs it waited for ended.
 * @param {string} gate @param {Readonly<Record<string, string>>} results by the name the step reads each by
 */
const partsVerdict = (gate, results) =>
  spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", script(gate, PARTS)], {
    env: { PATH: process.env.PATH ?? "", ...results },
    encoding: "utf8",
  }).status;

/** @param {Readonly<Record<string, string>>} waits @param {string} result @returns {Record<string, string>} */
const all = (waits, result) => Object.fromEntries(Object.keys(waits).map((name) => [name, result]));

test("a gate that judges shares asks how every part ended before it judges their records", () => {
  for (const { count, gate, combine, waits } of SHARED) {
    const lines = jobs.get(gate) ?? "";
    assert.equal(own(gate, "needs"), `[changes, ${Object.values(waits).join(", ")}]`);
    for (const [name, job] of Object.entries(waits)) {
      assert.ok(lines.includes(`\n          ${name}: \${{ needs.${job}.result }}\n`), `${gate}: ${name}`);
    }
    assert.ok(lines.includes(`\n          ${combine}: \${{ env.${count} }}\n`), `${gate}: ${combine}`);
    assert.ok(lines.indexOf(`- name: ${PARTS}\n`) < lines.indexOf("- uses: "), `${gate}: it must be the first step`);
    assert.equal(partsVerdict(gate, all(waits, "success")), 0, gate);
  }
});

test("a cancelled run leaves a gate that judges shares failed, never skipped", () => {
  for (const { gate, waits } of SHARED) {
    // A job whose condition is false is skipped, and a skipped job passes a
    // required check. `!cancelled()` is false once the run is cancelled, so the
    // job that waits for the parts must not carry it: it starts, and its
    // first step, which runs whatever happened, fails on what the parts report.
    assert.match(own(gate, "if") ?? "", /^\$\{\{ always\(\) && /, gate);
    assert.equal(stepCondition(gate, PARTS), "${{ always() }}", gate);
    // What GitHub gives as `needs.<job>.result`: a part that ran out of time is a failure.
    for (const other of ["cancelled", "failure", "skipped", ""]) {
      for (const name of Object.keys(waits)) {
        const one = { ...all(waits, "success"), [name]: other };
        assert.notEqual(partsVerdict(gate, one), 0, `${gate}: ${name} ${other || "unknown"}`);
      }
      assert.notEqual(partsVerdict(gate, all(waits, other)), 0, `${gate}: all ${other || "unknown"}`);
    }
    // A result that was never handed over is not a success either.
    assert.notEqual(partsVerdict(gate, {}), 0, `${gate}: nothing known`);
    // No other step of that job may run on a cancelled run, nor be what decides.
    const later = (jobs.get(gate) ?? "").split(/^ {6}- /m).slice(2);
    assert.ok(later.length >= 5, gate);
    for (const step of later) assert.match(step, /^ {8}if: \$\{\{ !cancelled\(\) \}\}$/m, step.split("\n")[0]);
  }
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
  assert.ok(added >= 10, `${added} steps of the report were read`);
});

// --- The endurance of the whole console (ANH-164; docs/framework-de-test.md, section 17) ---
//
// 24 simulated hours take hours of a runner. They run on the nightly event
// alone, as one step of the job of the simulation reports; every other run has
// the short form, a test of the battery. A later edit could move the day to
// every pull request, soften its failure, or stop keeping its report.
test("the 24 simulated hours of the console run at night only, fail their gate and leave their report", () => {
  const steps = stepsOf("simulation-report");
  const endurance = steps.filter((text) => /^ {8}run: .*-m simulation\.endurance /m.test(text));
  assert.equal(endurance.length, 1);
  const step = endurance[0] ?? "";
  assert.ok(
    step.includes(
      "\n        run: raspberry-pi/.venv/bin/python -m simulation.endurance simulated --hours 24 --out simulation/out/ci/endurance\n",
    ),
  );
  assert.match(step, /^ {8}if: \$\{\{ github\.event_name == 'schedule' \}\}$/m);
  // No other job runs it: a pull request never waits for a simulated day.
  assert.equal(workflow.split("-m simulation.endurance").length - 1, 1);
  // Its failure is the failure of the job, which `simulation-gate` waits for and requires.
  assert.doesNotMatch(step, /continue-on-error/);
  assert.match(own("simulation-gate", "needs") ?? "", /\bsimulation-report\b/);
  // It has a limit of its own, and the job's is that limit plus the one of the synthetic report.
  const minutes = (/** @type {string} */ text) => Number(/^ {8}timeout-minutes: (\d+)$/m.exec(text)?.[1]);
  const synthetic = steps.find((text) => text.startsWith("name: Synthetic simulation report\n")) ?? "";
  assert.ok(minutes(step) >= 120, "a simulated day does not fit in less on a runner");
  assert.equal(Number(own("simulation-report", "timeout-minutes")), minutes(synthetic) + minutes(step));
  // The report is kept with the synthetic ones, after the day ran and whatever its verdict.
  const KEPT = "Keep the synthetic reports";
  const kept = steps.find((text) => text.startsWith(`name: ${KEPT}\n`)) ?? "";
  assert.ok(steps.indexOf(kept) > steps.indexOf(step));
  assert.equal(stepCondition("simulation-report", KEPT), "${{ !cancelled() }}");
  for (const file of ["endurance.json", "endurance.md"]) {
    assert.ok(kept.includes(`\n            simulation/out/ci/endurance/${file}\n`), file);
  }
  // The short form every run has: the same scenario, a few simulated minutes, in the battery.
  assert.match(
    atRoot("simulation/tests/test_endurance.py"),
    /^async def test_ex1_a_short_simulated_day_chains_manual_and_programmed_sessions_and_measures_them\(/m,
  );
  // And 24 hours is what the command runs when it is not told how many.
  assert.match(atRoot("simulation/endurance.py"), /"--hours", type=float, default=24\.0/);
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
  assert.match(atRoot("vitest.lib.config.mts"), /^ {4}include: testsOf\(SITE\.suites\.lib\),$/m);
  assert.match(atRoot("vitest.site.config.mts"), /^ {4}include: testsOf\(SITE\.suites\.site\),$/m);
  assert.deepEqual(siteFolders(), [...SITE.suites.lib, ...SITE.suites.site]);
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
  for (const name of ["test:convex", "test:lib", "test:site"]) {
    assert.doesNotMatch(scripts[name], /coverage/, `npm run ${name} measures the coverage`);
  }
  // The two suites of the site hold no measure of their own: one run measures the site.
  for (const config of ["vitest.lib.config.mts", "vitest.site.config.mts"]) {
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

test("every npm script the documentation or a workflow names is a script of package.json", () => {
  // A script that is renamed (`test:ecg` became `test:lib`, ANH-183) leaves its old name in pages and
  // in steps written meanwhile on other branches: the command they give would not start.
  const scripts = Object.keys(JSON.parse(atRoot("package.json")).scripts);
  const root = new URL("../../", import.meta.url);
  const pages = readdirSync(new URL("docs/", root), { recursive: true, encoding: "utf8" })
    .filter((name) => name.endsWith(".md"))
    .map((name) => `docs/${name.split("\\").join("/")}`);
  const workflows = readdirSync(new URL(".github/workflows/", root)).map((name) => `.github/workflows/${name}`);
  assert.ok(pages.length >= 10 && workflows.length >= 5, "the documentation and the workflows were not read");
  let named = 0;
  for (const file of ["README.md", "raspberry-pi/README.md", ...pages, ...workflows]) {
    // `npm run test:*` names a family of scripts: one of them at least must exist.
    for (const [, name = "", family] of atRoot(file).matchAll(/\bnpm run ([a-z][\w:-]*)(\*)?/g)) {
      named += 1;
      const defined = family ? scripts.some((script) => script.startsWith(name)) : scripts.includes(name);
      assert.ok(defined, `${file} names \`npm run ${name}${family ?? ""}\`, which package.json does not define`);
    }
  }
  assert.ok(named >= 30, `${named} mentions of a script were read`);
  assert.ok(scripts.includes("test:lib") && !scripts.includes("test:ecg"));
});

test("the Pi gate still judges every test once and the whole coverage, from what its two parts left", () => {
  const tests = jobs.get("pi-tests") ?? "";
  const gate = jobs.get("pi-gate") ?? "";
  // The matrix job reports as `pi (tests 1)` and `pi (tests 2)`: the required name stays the gate's alone.
  assert.equal(own("pi-tests", "name"), "pi (${{ matrix.part }})");
  assert.match(tests, /^ {6}fail-fast: false$/m, "a part that fails must not cancel the other");
  // Both run the one definition of the gate, and so does the job that judges them.
  for (const [id, name] of [
    ["pi-tests", "Pi gate, shares ${{ matrix.shares }} of the tests"],
    ["pi-gate", "Pi gate, every test once and combined coverage"],
  ]) {
    const step = stepsOf(id ?? "").find((text) => text.startsWith(`name: ${name}\n`)) ?? "";
    assert.match(step, /^ *(run: )?bash raspberry-pi\/scripts\/check\.sh$/m, `${id}: ${name}`);
    assert.doesNotMatch(step, /continue-on-error|PI_GATE_PROCESSES|--cov|fail-under/, `${id}: ${name}`);
  }
  // What a part recorded and measured reaches the gate: hidden files too, the measure is named `.coverage`.
  assert.match(tests, /^ {10}PI_GATE_EVIDENCE: \$\{\{ runner\.temp \}\}\/pi-evidence$/m);
  const KEPT = "Keep what these shares recorded and measured";
  const kept = stepsOf("pi-tests").find((text) => text.startsWith(`name: ${KEPT}\n`)) ?? "";
  for (const setting of [
    "name: pi-evidence-${{ matrix.shares }}",
    "path: ${{ runner.temp }}/pi-evidence",
    "if-no-files-found: error",
    "overwrite: true",
    "include-hidden-files: true",
  ]) {
    assert.ok(kept.includes(`\n          ${setting}\n`), setting);
  }
  assert.equal(stepCondition("pi-tests", KEPT), "${{ !cancelled() }}");
  const gathered = stepsOf("pi-gate").find((text) => text.startsWith("name: Gather what every share")) ?? "";
  for (const setting of ["pattern: pi-evidence-*", "merge-multiple: true", "path: ${{ runner.temp }}/pi-evidence"]) {
    assert.ok(gathered.includes(`\n          ${setting}\n`), setting);
  }
  assert.match(gate, /^ {10}PI_GATE_EVIDENCE: \$\{\{ runner\.temp \}\}\/pi-evidence$/m);
  // The threshold is the gate script's, applied where every share is gathered: 100 % of branches.
  const check = atRoot("raspberry-pi/scripts/check.sh");
  assert.ok(check.includes(' --combine "$COMBINE" --evidence "$EVIDENCE" --fail-under 100 '));
  assert.ok(check.includes(' --shares "$SHARES" --evidence "$EVIDENCE" ${REPORT:+'), "a part applies no threshold");
  // The nightly endurance of the session record is a step of one part: see the test below.
  assert.doesNotMatch(gate, /pytest tests\//, "the job that judges runs no test of the console");
});

// --- The nightly endurance of the session record (ANH-128, ANH-218) ---
//
// A simulated day of sessions with the record written, in one step of one part of the Pi tests. It
// had never run since the Pi gate was cut in two jobs: the nightly event starts on the default
// branch only, and no manual run could reach the step. What follows holds that the step exists, that
// the night starts it, that a manual run starts it when it is asked to and never otherwise, that the
// release pipeline cannot, and that the command it runs still selects the test it is there for. That
// last point is not a detail: a marker or a variable renamed on one side only leaves a step that
// passes having run nothing.

test("the nightly endurance runs every night, on a manual run that asks for it, and nowhere else", () => {
  const tests = jobs.get("pi-tests") ?? "";
  const triggers = workflow.slice(0, workflow.indexOf("\npermissions:"));
  const endurance = stepsOf("pi-tests").filter((text) => /^ {8}run: .*test_record_endurance\.py/m.test(text));
  assert.equal(endurance.length, 1, "one step of the Pi tests runs the endurance");
  const step = endurance[0] ?? "";
  // One part only: the matrix names it, and the step asks the matrix.
  assert.equal(tests.match(/^ {12}endurance: true$/gm)?.length, 1, "one part runs the endurance");
  assert.equal(tests.match(/^ {12}endurance: false$/gm)?.length, 1, "the other part says it does not");
  // The night starts it. A manual run starts it only with the box ticked: compared with `true`, so
  // that the text "false" is not a ticked box. No other event can.
  assert.match(triggers, /^ {2}schedule:\n {4}- cron: '\d+ \d+ \* \* \*'$/m, "the workflow no longer runs every night");
  assert.equal(
    /^ {8}if:[ \t]*(.*)$/m.exec(step)?.[1],
    "${{ (github.event_name == 'schedule' || inputs.endurance == true) && matrix.endurance }}",
  );
  const box = /^ {2}workflow_dispatch:\n {4}inputs:\n((?: {6}.*\n)+)/m.exec(triggers)?.[1] ?? "";
  assert.deepEqual(
    [...box.matchAll(/^ {6}([a-z_]+):$/gm)].map(([, name]) => name),
    ["endurance"],
    "the inputs of a manual run",
  );
  assert.match(box, /^ {8}type: boolean$/m);
  assert.match(box, /^ {8}default: false$/m, "a manual run costs what it always did unless it asks");
  assert.match(box, /^ {8}required: false$/m);
  // A workflow that calls this one (the release pipeline) can hand it no input: the box is empty there.
  assert.match(triggers, /^ {2}workflow_call:$/m);
  assert.doesNotMatch(triggers, /^ {2}workflow_call:\n {4}/m, "a caller could ask for the endurance");
  assert.equal(workflow.split("inputs.").length - 1, 2, "`inputs.` is read in the comment and in the condition of the step");
  // Its failure fails the part, hence `pi-gate`; and it has the time the documentation gives it.
  assert.doesNotMatch(step, /continue-on-error/);
  const limit = Number(/^ {8}timeout-minutes: (\d+)$/m.exec(step)?.[1]);
  const budget = Number(own("pi-tests", "timeout-minutes"));
  assert.equal(limit, 35);
  assert.ok(budget - limit >= 20, `the tests of the part have ${budget - limit} minutes before the endurance`);
  // What it runs: the one test marked `slow`, with the variable that test waits for. From raspberry-pi/,
  // with the environment the step before it installed.
  assert.match(step, /^ {8}working-directory: raspberry-pi$/m);
  assert.match(step, /^ {8}run: \.venv\/bin\/python -m pytest tests\/test_record_endurance\.py -m slow -s$/m);
  const hours = /^ {10}([A-Z_]+): '24'$/m.exec(step)?.[1];
  assert.equal(hours, "ANHEART_ENDURANCE_HOURS");
  const file = atRoot("raspberry-pi/tests/test_record_endurance.py");
  assert.match(file, new RegExp(`^HOURS_KEY: Final\\[str\\] = "${hours}"$`, "m"), "the test waits for another variable");
  assert.match(file, /^NIGHTLY_HOURS: Final\[float\] = float\(os\.environ\.get\(HOURS_KEY, "0"\) or "0"\)$/m);
  // One test carries the marker, and it is the one skipped without the variable: `-m slow` selects it alone.
  assert.equal(file.match(/^@pytest\.mark\.slow$/gm)?.length, 1);
  assert.match(
    file,
    /^@pytest\.mark\.slow\n@pytest\.mark\.skipif\(NIGHTLY_HOURS <= 0, .*\)\nasync def test_ex10_a_day_of_console_in_simulation_/m,
  );
  // The marker is one pytest knows: with `--strict-markers` an unknown one stops the collection.
  assert.match(atRoot("raspberry-pi/pyproject.toml"), /^ {4}"slow: /m);
});

test("all five Python gates install only the hashed development locks", () => {
  const installing = [...jobs.keys()].filter((id) => stepsOf(id).some((step) => step.startsWith("name: Install Python dependencies\n")));
  assert.deepEqual(installing, ["pi-tests", "pi-gate", "simulation-battery", "simulation-report", "simulation-gate"]);
  for (const id of installing) {
    assert.equal(script(id, "Install Python dependencies"), [
      "uv venv raspberry-pi/.venv",
      "uv pip install --python raspberry-pi/.venv/bin/python --require-hashes -r raspberry-pi/requirements-dev-lock.txt",
      "uv pip install --python raspberry-pi/.venv/bin/python --require-hashes --no-deps -r raspberry-pi/requirements-dev-nodeps-lock.txt",
      "",
    ].join("\n"), id);
  }
});

test("Python installation in every workflow requires hashes except the named CAD extractor", () => {
  const exceptions = [];
  for (const file of readdirSync(new URL("../../.github/workflows/", import.meta.url))) {
    const text = atRoot(`.github/workflows/${file}`).replaceAll(/\\\n\s*/g, " ");
    for (const step of text.split(/^ {6}- /m)) {
      for (const line of step.split("\n").filter((line) => !line.trimStart().startsWith("#") && /\bpip install\b/.test(line))) {
        if (/\s--require-hashes(?:\s|$)/.test(line)) continue;
        assert.ok(step.startsWith("name: Install and verify CAD extractor\n"), `${file}: ${line}`);
        assert.equal(line.trim(), "uv pip install --python simulation/cad/.venv-cad/bin/python -r simulation/cad/requirements.txt");
        exceptions.push(file);
      }
    }
  }
  assert.deepEqual(exceptions, ["ci.yml"]);
});

test("simulation-gate installs the assembled hashed payload and serves it without the repository Python path", () => {
  const id = "simulation-gate";
  const name = "Install the hosted simulation as Vercel receives it, hashes required";
  const steps = stepsOf(id);
  const at = steps.findIndex((step) => step.startsWith(`name: ${name}\n`));
  assert.ok(at > 0);
  assert.ok(steps[at - 1]?.startsWith("name: Test the hosted simulation engine\n"));
  assert.equal(stepCondition(id, name), "${{ !cancelled() }}");
  assert.ok(!softened(steps[at] ?? ""));
  const smoke = script(id, name);
  assert.match(smoke, /^bash deploy\/simulation-vercel\/build\.sh\nhosted_venv="\$RUNNER_TEMP\/hosted-simulation-venv"\nuv venv "\$hosted_venv"\n/m);
  assert.match(smoke, /^uv pip install --python "\$hosted_venv\/bin\/python" --require-hashes -r deploy\/simulation-vercel\/dist\/requirements\.txt$/m);
  assert.match(smoke, /^cd deploy\/simulation-vercel\/dist\nenv -u PYTHONPATH "\$hosted_venv\/bin\/python" - <<'PY'\n/m);
  assert.match(smoke, /^from app import app\nfrom fastapi\.testclient import TestClient$/m);
  assert.match(smoke, /^with TestClient\(app\) as client:\n {4}response = client\.get\("\/api\/scenarios"\)\n {4}print\(response\.status_code\)\n {4}if response\.status_code != 200:\n {8}raise SystemExit\(1\)\nPY\n$/m);
  assert.doesNotMatch(smoke, /\|\|\s*(true|:)|set \+e/);
});

// --- The hosted simulation engine, tested by a gate that existed (ANH-218) ---
//
// deploy/simulation-vercel/tests drive the wrapper Vercel serves, with the real simulation runtime
// behind it. No job ran them: the wrapper could stop importing, or stop refusing a name it must
// refuse, with every check green. They have no job of their own, for the reason the release tooling
// has none: branch protection lists the required checks by name, and a step of a required job fails
// that job.

test("the hosted simulation engine is tested by `simulation-gate`, and nothing softens the step", () => {
  const GATE = "simulation-gate";
  const name = "Test the hosted simulation engine";
  const steps = stepsOf(GATE);
  const at = steps.findIndex((text) => text.startsWith(`name: ${name}\n`));
  assert.ok(at > 0, `${GATE}: no step "${name}"`);
  const step = steps[at] ?? "";
  // The whole directory, from the repository root, with the interpreter the job installed.
  assert.match(step, /^ {8}run: raspberry-pi\/\.venv\/bin\/python -m pytest deploy\/simulation-vercel\/tests$/m);
  assert.doesNotMatch(step, /working-directory/, "the root is where pyproject.toml gives pytest its paths");
  const install = steps.findIndex((text) => text.startsWith("name: Install Python dependencies\n"));
  assert.ok(install >= 0 && install < at, "the environment must be installed first");
  // It fails the job when a test fails, runs whatever the steps before it found, never on a cancelled run.
  assert.ok(!softened(step), "the step can pass though a test failed");
  assert.equal(stepCondition(GATE, name), "${{ !cancelled() }}");
  // The job is a required check, and the path rule can skip it only as it skips the simulation itself.
  assert.ok(REQUIRED.includes(GATE));
  assert.equal(own(GATE, "if"), GATED.get(GATE));
  // No other step of the workflow runs them: once, in one place.
  const running = [...jobs].flatMap(([id]) =>
    stepsOf(id)
      .filter((text) => /^ {8,10}(run: )?.*\bpytest\b.*deploy\/simulation-vercel/m.test(text))
      .map((text) => `${id}: ${titleOf(text)}`),
  );
  assert.deepEqual(running, [`${GATE}: name: ${name}`]);
  // What the command relies on: the directory holds the tests, and the root manifest names the paths
  // they import from (the wrapper, the simulation package, the code of the Pi).
  const files = readdirSync(new URL("../../deploy/simulation-vercel/tests/", import.meta.url));
  assert.ok(files.includes("test_app.py") && files.includes("test_dependencies.py"), files.join(", "));
  const manifest = atRoot("pyproject.toml");
  assert.match(manifest, /^testpaths = \["deploy\/simulation-vercel\/tests"\]$/m);
  assert.match(manifest, /^pythonpath = \["\.", "raspberry-pi", "deploy\/simulation-vercel"\]$/m);
  // The job installs nothing of Vercel and holds no token: these tests reach no deployment.
  assert.doesNotMatch(jobs.get(GATE) ?? "", /vercel (deploy|pull|build)|VERCEL_TOKEN/i);
});

test("the Pi gate script refuses two ways of running the tests at once, before it runs anything", () => {
  const gateScript = new URL("../../raspberry-pi/scripts/check.sh", import.meta.url).pathname;
  const run = (/** @type {Record<string, string>} */ settings) =>
    spawnSync("bash", ["--noprofile", "--norc", gateScript], {
      env: { PATH: process.env.PATH ?? "", ...settings },
      encoding: "utf8",
    });
  const kept = { PI_GATE_EVIDENCE: "kept" };
  for (const [settings, said] of /** @type {[Record<string, string>, RegExp][]} */ ([
    [{ PI_GATE_SHARES: "0-3/8", PI_GATE_COMBINE: "8", ...kept }, /three ways to run the tests: set one/],
    [{ PI_GATE_PROCESSES: "4", PI_GATE_SHARES: "0-3/8", ...kept }, /three ways to run the tests: set one/],
    [{ PI_GATE_PROCESSES: "4", PI_GATE_COMBINE: "8", ...kept }, /three ways to run the tests: set one/],
    [{ PI_GATE_SHARES: "0-3/8" }, /PI_GATE_EVIDENCE must name the directory/],
    [{ PI_GATE_COMBINE: "8" }, /PI_GATE_EVIDENCE must name the directory/],
    [kept, /PI_GATE_EVIDENCE goes with PI_GATE_SHARES or PI_GATE_COMBINE/],
    [{ PI_GATE_PROCESSES: "4", ...kept }, /PI_GATE_EVIDENCE goes with PI_GATE_SHARES or PI_GATE_COMBINE/],
  ])) {
    const ran = run(settings);
    assert.equal(ran.status, 1, JSON.stringify(settings));
    assert.match(ran.stderr, said, JSON.stringify(settings));
    assert.equal(ran.stdout, "", `a stage ran: ${JSON.stringify(settings)}`);
  }
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
  // The gate scripts are told where to leave what the report reads: next to the evidence
  // of each job that runs shares of a suite, so that it reaches the gate with it.
  for (const [runs, gate, evidence, count] of [
    ["pi-tests", "pi-gate", "pi-evidence", "PI_SHARES"],
    ["simulation-battery", "simulation-gate", "simulation-evidence", "SIMULATION_SHARES"],
  ]) {
    const judging = jobs.get(gate ?? "") ?? "";
    assert.match(judging, /^ {10}QUALITY_REPORT_DIR: \$\{\{ runner\.temp \}\}\/quality$/m, gate);
    assert.ok(
      (jobs.get(runs ?? "") ?? "").includes(
        `\n          QUALITY_REPORT_DIR: \${{ runner.temp }}/${evidence}/quality-\${{ matrix.shares }}\n`,
      ),
      runs,
    );
    // As many JUnit files as shares: the count each gate gives is the one its suite was cut into.
    assert.ok(judging.includes(` --parts "$RUNNER_TEMP/${evidence}" `), gate);
    assert.ok(judging.includes(` --shares "$${count}"\n`), gate);
  }
  assert.ok(
    (jobs.get("pi-gate") ?? "").includes(
      'quality-report.mjs job pi --dir "$RUNNER_TEMP/quality" --parts "$RUNNER_TEMP/pi-evidence" --shares "$PI_SHARES"\n',
    ),
  );
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
  assert.deepEqual(kept(jobs.get("docs") ?? ""), ["scripts-ci.xml", "scripts-men.xml", "scripts-release.xml"]);
  // The job that decides which gates run runs no test and hands the report nothing.
  assert.deepEqual(kept(jobs.get("changes") ?? ""), []);
  // Each of those files is written by `node --test` itself, for the test file named on the same line.
  for (const [id, file] of [
    ["audit", "scripts/ci/braces-depth-guard.test.mjs"],
    ["audit", "scripts/ci/gitleaks-fixture.test.mjs"],
    ["docs", "scripts/ci/check-men.test.mjs"],
    ["docs", "scripts/release.test.mjs"],
    ["docs", "scripts/ci/convex-generated-api.test.mjs"],
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

// --- The release tooling, tested by a gate that existed (ANH-195) ---
//
// It has no job of its own: branch protection lists the required checks by
// name, and a step of a required job fails that job. What follows holds that
// the step is one nothing softens, in a job that runs whatever the files changed.

test("the release tooling is tested by `docs`, the required job no path rule can skip", () => {
  const name = "Test the release tooling";
  const step = stepsOf("docs").find((text) => text.startsWith(`name: ${name}\n`)) ?? "";
  assert.ok(step !== "", `docs: no step "${name}"`);
  assert.doesNotMatch(step, /continue-on-error/);
  // After the checks of the pages, whatever they found; never on a cancelled run.
  assert.equal(stepCondition("docs", name), "${{ !cancelled() }}");
  assert.match(script("docs", name), / scripts\/release\.test\.mjs$/m);
  assert.ok(REQUIRED.includes("docs") && ALWAYS.includes("docs"));
  // It is the test file `npm run test:release` names, and the report counts it for the job that runs it.
  assert.equal(JSON.parse(atRoot("package.json")).scripts["test:release"], "node --test scripts/release.test.mjs");
  assert.deepEqual(
    SUITES.filter(({ job }) => job === "docs").map(({ id }) => id),
    ["scripts-ci", "scripts-men", "scripts-release"],
  );
});

// --- What tests the gates is itself a gate (ANH-183) ---
//
// The tests of the path rule and of the workflows once ran in `changes`, which
// branch protection does not require: one of them failing blocked no merge. And
// the path rule lets `web` be skipped on a pull request that changes no file of
// the site, though one test of the site reads every file of the repository.
// Both now run in `docs`: a required check, on every event, whatever changed.

/** The steps of a job that can let it pass though a command of theirs failed. @param {string} step */
const softened = (step) => /continue-on-error|\|\| *(true|:)\b|set \+e\b/.test(step);

test("every test file of scripts/ci runs in a required job that no path rule can skip", () => {
  const always = REQUIRED.filter((id) => ALWAYS.includes(id));
  assert.deepEqual(always, ["audit", "docs"]);
  const files = readdirSync(new URL("./", import.meta.url)).filter((name) => name.endsWith(".test.mjs"));
  assert.ok(files.length >= 9 && files.includes("ci-workflow.test.mjs"), "the test files of scripts/ci were not read");
  for (const file of files) {
    const running = always.flatMap((id) =>
      stepsOf(id)
        .filter((step) =>
          new RegExp(`^ +(run: )?.*\\bnode --test .* scripts/ci/${file.replaceAll(".", "\\.")}\\b`, "m").test(step),
        )
        .map((step) => ({ id, step })),
    );
    assert.equal(running.length, 1, `${file} is run by ${running.length} steps of ${always.join(" and ")}`);
    const [{ id, step }] = /** @type {[{id: string, step: string}]} */ (running);
    assert.ok(!softened(step), `${id}: the step that runs ${file} can pass though it failed`);
    // Never skipped because an earlier check of the job failed; never run on a cancelled run.
    const condition = /^ {8}if:[ \t]*(.*)$/m.exec(step)?.[1];
    assert.equal(condition, "${{ !cancelled() }}", `${id}: ${file}`);
  }
  // The job that decides which gates run is not required: it runs no test, so none can fail unseen.
  assert.ok(!REQUIRED.includes("changes"));
  assert.doesNotMatch(jobs.get("changes") ?? "", /node --test|\.test\.mjs/);
  assert.deepEqual(
    SUITES.filter(({ id }) => id === "scripts-ci").map(({ job, part }) => [job, part]),
    [["docs", "scripts-docs"]],
  );
});

test("the test that reads every file of the repository runs in `docs`, whatever the pull request changes", () => {
  const WHOLE = "lib/legacyModeReferences.test.ts";
  const name = "Test what is read across the whole repository";
  const steps = stepsOf("docs");
  const at = steps.findIndex((text) => text.startsWith(`name: ${name}\n`));
  assert.ok(at > 0, `docs: no step "${name}"`);
  const step = steps[at] ?? "";
  assert.match(step, new RegExp(`^ {8}run: npm run test:lib -- ${WHOLE.replaceAll(".", "\\.")}$`, "m"));
  assert.ok(!softened(step), "the step can pass though the test failed");
  assert.equal(stepCondition("docs", name), "${{ !cancelled() }}");
  // The command exists, runs the file, and the file is the one that reads the repository.
  assert.equal(JSON.parse(atRoot("package.json")).scripts["test:lib"], "vitest run --config vitest.lib.config.mts");
  assert.match(atRoot(WHOLE), /execFileSync\("git", \["ls-files", "-z"\]/);
  assert.ok(
    testsOf(SITE.suites.lib).some((glob) => globToRegExp(glob).test(WHOLE)),
    "the suite does not run it",
  );
  // What it runs with is installed just before, from the lockfile, and nothing softens that either.
  const before = steps.slice(0, at);
  const install = before.findIndex((text) => /^ {8}run: npm ci$/m.test(text));
  const node = before.findIndex((text) => text.startsWith("uses: actions/setup-node@"));
  assert.ok(node >= 0 && install > node, "docs: `npm ci` must follow the installation of Node");
  for (const index of [node, install]) {
    assert.ok(!softened(before[index] ?? ""));
    assert.match(before[index] ?? "", /^ {8}if: \$\{\{ !cancelled\(\) \}\}$/m);
  }
  // The checks that install nothing stay first: none of them waits for, nor depends on, the install.
  const cheap = before.slice(0, node).map((text) => titleOf(text));
  for (const title of [
    "Check local Markdown links and anchors",
    "Resolve threat identifiers",
    "Test the release tooling",
  ]) {
    assert.ok(cheap.includes(`name: ${title}`), `${title} must come before the install`);
  }
  // `web` still runs the whole suite of lib/, this file included, when the site changes.
  assert.match(jobs.get("web") ?? "", /^ {6}- run: npm run test:lib -- \$VITEST_REPORT /m);
});

test("the workflow gives no token more than read access, and each action is pinned to one full commit", () => {
  assert.match(workflow, /^permissions:\n {2}contents: read\n\n/m);
  assert.equal(workflow.split(/^ *permissions:/m).length - 1, 1, "a job asks for permissions of its own");
  // One secret, read by the browser suite alone (see its own test below).
  assert.equal(workflow.split(E2E_SECRET).length - 1, 1);
  assert.doesNotMatch(workflow.replace(E2E_SECRET, ""), /pull_request_target|workflow_run|\bsecrets\.|: write\b|write-all/);
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

test("the browser suite is no gate, is skipped without its variable, and alone reads the one secret", () => {
  assert.ok(!REQUIRED.includes(E2E));
  assert.equal(own(E2E, "name"), undefined);
  assert.equal(own(E2E, "strategy"), undefined);
  assert.equal(own(E2E, "needs"), "changes");
  // `vars` is the one context of the job's own condition that can tell the
  // suite has its instance: `secrets` cannot be read there, nor the
  // variables of an environment, and an unset variable reads as ''.
  assert.equal(own(E2E, "if"), E2E_CONDITION);
  assert.equal(own(E2E, "environment"), "e2e");
  // The secret reaches the suite through the environment of its step, the step that runs it.
  const suite = stepsOf(E2E).find((step) => step.startsWith("name: Build the site, then run the end-to-end suite\n")) ?? "";
  assert.ok(suite.includes(E2E_SECRET), "the secret is read by the step that runs the suite");
  assert.match(suite, /^ {8}run: npm run test:e2e$/m);
  for (const [id] of jobs) if (id !== E2E) assert.doesNotMatch(jobs.get(id) ?? "", /\bsecrets\./, id);
  // What it leaves, whatever the outcome of its tests, unless the suite found
  // a secret in it: then its marker is there, and nothing is sent.
  // The positive marker is required: a suite that died before its count
  // wrote neither, and sends nothing.
  assert.ok(E2E_DO_NOT_SEND && E2E_MAY_SEND, "e2e/secret-scan.ts names its two markers");
  const check = "Check that the suite allows its outputs to be sent";
  assert.equal(stepCondition(E2E, check), "${{ !cancelled() }}");
  assert.equal(
    script(E2E, check),
    `if [ -e "$OUTPUT/${E2E_MAY_SEND}" ] && [ ! -e "$OUTPUT/${E2E_DO_NOT_SEND}" ]; then\n` +
      '  echo "send=true" >> "$GITHUB_OUTPUT"\n' +
      "else\n" +
      '  echo "::warning title=Sorties de la suite retenues::la recherche des secrets n\'a pas fini sans rien trouver (occurrence, comptage impossible ou suite interrompue) : rien n\'est envoyé."\n' +
      "fi\n",
  );
  const kept = stepsOf(E2E).filter((step) => step.includes("actions/upload-artifact@"));
  assert.deepEqual(
    kept.map((step) => /^ {10}name: (.+)$/m.exec(step)?.[1]),
    ["e2e-rapport", "e2e-traces", "e2e-captures"],
  );
  const steps = stepsOf(E2E);
  for (const step of kept) {
    assert.match(step, /^ {8}if: \$\{\{ !cancelled\(\) && steps\.outputs\.outputs\.send == 'true' \}\}$/m);
    assert.ok(steps.indexOf(step) > steps.findIndex((s) => s.startsWith(`name: ${check}\n`)), "the check comes first");
  }
  assert.match(steps.find((s) => s.startsWith(`name: ${check}\n`)) ?? "", /^ {8}id: outputs$/m);
});

test("the browser suite is no gate of a release, and one run at a time holds its Clerk instance", () => {
  // The release calls this workflow as its gates; a called workflow sees its
  // caller's name in `github.workflow`, so the suite is skipped there.
  assert.match(releaseWorkflow, /^ {4}uses: \.\/\.github\/workflows\/ci\.yml$/m);
  assert.notEqual(workflowName(releaseWorkflow), workflowName(workflow));
  assert.ok(E2E_CONDITION.includes(`github.workflow == '${workflowName(workflow)}'`));
  // A fork gets neither the secret nor the environment: skipped, not failed.
  assert.ok(E2E_CONDITION.includes("github.event.pull_request.head.repo.full_name == github.repository"));
  // Every start deletes what other runs left on the instance: one group for
  // the whole repository, a run waits and never cancels another.
  assert.ok(
    (jobs.get(E2E) ?? "").includes("    concurrency:\n      group: e2e-instance-clerk\n      cancel-in-progress: false\n"),
    "the suite's own concurrency group",
  );
});

// --- The dependency audit (ANH-153) ---
//
// `audit` is a required check that runs on every event. What it audits, the
// one place its exceptions are written, and the bill of materials it leaves.
// The exceptions themselves, Dependabot and the lock files of JavaScript are
// held by scripts/ci/dependency-audit.test.mjs.

/** The step of `audit` with this name. @param {string} name */
const auditStep = (name) => stepsOf("audit").find((text) => text.startsWith(`name: ${name}\n`)) ?? "";

test("the audit reads its exceptions first, and every audit it runs fails the required check on what it finds", () => {
  const titles = stepsOf("audit").map((step) => titleOf(step).replace(/^name: /, ""));
  const order = [
    "run: npm ci",
    "Read the exceptions of the audit",
    "Audit npm dependencies",
    "Verify dependency stack-depth guards",
    "Audit Python dependencies",
    "Scan all Git history",
    "Write the bill of materials",
    "Keep the bill of materials",
    "Keep the numbers for the quality report",
  ];
  assert.deepEqual(titles.slice(-order.length), order);
  assert.match(auditStep("Read the exceptions of the audit"), /^ {8}run: node scripts\/ci\/audit-exceptions\.mjs check$/m);
  // npm alone decides when it finds nothing; the exceptions are read only when it finds something.
  assert.equal(
    script("audit", "Audit npm dependencies"),
    "if npm audit --audit-level=high; then\n  exit 0\nfi\nnode scripts/ci/audit-exceptions.mjs npm\n",
  );
  // Each step runs after an earlier one failed, never on a cancelled run, and nothing lets one pass that failed.
  for (const name of order.slice(1, -1)) {
    assert.equal(stepCondition("audit", name), "${{ !cancelled() }}", name);
    assert.ok(!softened(auditStep(name)), `${name} can pass though it failed`);
  }
  // An exception is written in the table and nowhere else.
  for (const file of readdirSync(new URL("../../.github/workflows/", import.meta.url))) {
    assert.doesNotMatch(atRoot(`.github/workflows/${file}`), /--ignore-vuln|--omit[= ]dev|audit-level=(moderate|low|info|critical|none)/, file);
  }
});

/** The words of one `for <name> in ...; do` line of a script. @param {string} text @param {string} name */
const loopOver = (text, name) => new RegExp(`^for ${name} in (.+); do$`, "m").exec(text)?.[1]?.split(" ") ?? [];

test("pip-audit audits every Python environment the project installs, skipping only the exceptions of the table", () => {
  const python = script("audit", "Audit Python dependencies");
  assert.match(python, /^skipped="\$\(node scripts\/ci\/audit-exceptions\.mjs pip-audit-args\)"\nread -r -a ignore <<< "\$skipped"$/m);
  assert.equal(python.split("pip-audit==").length - 1, 1, "pip-audit is called once, by the function every environment goes through");
  assert.match(python, /uv tool run pip-audit==\d+\.\d+\.\d+ --disable-pip --require-hashes "\$\{ignore\[@\]\}" -r "\$2" \|\| failed\+=\("\$1"\)/);
  assert.match(python, /^if \(\( \$\{#failed\[@\]\} > 0 \)\); then\n.*\n {2}exit 1\nfi\n$/m);
  const locks = loopOver(python, "lock");
  const sets = loopOver(python, "set");
  assert.deepEqual(locks, ["raspberry-pi/requirements-lock.txt", "raspberry-pi/requirements-build-lock.txt", "raspberry-pi/requirements-dev-lock.txt", "raspberry-pi/requirements-dev-nodeps-lock.txt", "deploy/simulation-vercel/requirements-lock.txt"]);
  assert.deepEqual(sets, ["simulation/cad/requirements.txt"]);
  assert.match(python, /^ {2}uv pip compile "\$set" --python-version 3\.12 --generate-hashes --quiet --output-file "\$resolved"$/m);
  // A lock file audited as it is fixes every version and every hash: pip-audit reads nothing it would resolve.
  for (const lock of locks) {
    const requirements = atRoot(lock).split("\n").filter((line) => /^[A-Za-z0-9]/.test(line));
    assert.ok(requirements.length > 0 && requirements.every((line) => /^[\w.-]+==[\w.+!-]+(?: ; [^\r\n\\]+)? \\$/.test(line)), lock);
    assert.equal(atRoot(lock).split("--hash=sha256:").length - 1 >= requirements.length, true, lock);
  }
  // Every requirements file of the repository is one of those, is included by one (`-r`), or is
  // the file a lock was compiled from (the command at its top).
  /** @type {Set<string>} */
  const reached = new Set();
  const visit = (/** @type {string} */ path) => {
    if (reached.has(path)) return;
    reached.add(path);
    const text = atRoot(path);
    for (const [, included = ""] of text.matchAll(/^-r +(\S+)$/gm)) visit(join(dirname(path), included));
    const compiled = /^# {4}uv pip compile (\S+) /m.exec(text)?.[1];
    if (compiled) visit(join(dirname(path), compiled));
  };
  for (const path of [...locks, ...sets]) visit(path);
  const files = spawnSync("git", ["ls-files", "-z", "--", "*requirements*.txt"], { encoding: "utf8", cwd: new URL("../..", import.meta.url) })
    .stdout.split("\0")
    .filter(Boolean);
  assert.ok(files.length >= 10, "the requirements files of the repository were not read");
  for (const path of files) {
    const text = atRoot(path);
    // A file that only includes others, like the requirements.txt of the root, adds nothing to audit.
    const own = text.split("\n").filter((line) => line.trim() !== "" && !line.startsWith("#"));
    const onlyIncludes = own.every((line) => /^-r +\S+$/.test(line)) && own.every((line) => reached.has(join(dirname(path), line.slice(3).trim())));
    assert.ok(reached.has(path) || onlyIncludes, `${path} is installed somewhere and audited nowhere`);
  }
});

test("the bill of materials is written from the lock files on every run, and kept with the run", () => {
  const written = script("audit", "Write the bill of materials");
  assert.match(written, /^npm sbom --sbom-format cyclonedx --package-lock-only > "\$RUNNER_TEMP\/sbom\/site-convex\.cdx\.json"$/m);
  assert.deepEqual(loopOver(written, "lock"), ["requirements-lock", "requirements-build-lock"]);
  assert.match(written, /uv tool run --from cyclonedx-bom==\d+\.\d+\.\d+ cyclonedx-py requirements "raspberry-pi\/\$lock\.txt" --output-format JSON --output-file "\$RUNNER_TEMP\/sbom\/pi-\$lock\.cdx\.json"$/m);
  assert.match(written, /^uv tool run --from cyclonedx-bom==7\.4\.0 cyclonedx-py requirements deploy\/simulation-vercel\/requirements-lock\.txt --output-format JSON --output-file "\$RUNNER_TEMP\/sbom\/simulation-requirements-lock\.cdx\.json"$/m);
  const kept = auditStep("Keep the bill of materials");
  assert.match(kept, /^ {8}uses: actions\/upload-artifact@/m);
  assert.deepEqual(
    kept.split("\n").filter((line) => /^ {10}\S/.test(line)).map((line) => line.trim()),
    ["name: sbom", "retention-days: 90", "if-no-files-found: error", "overwrite: true", "path: ${{ runner.temp }}/sbom"],
  );
  // The release pipeline calls this workflow, so its run keeps the bill of materials of the commit it tags.
  assert.match(atRoot(".github/workflows/release.yml"), /^ {4}uses: \.\/\.github\/workflows\/ci\.yml$/m);
  assert.match(atRoot("docs/release.md"), /artefact `sbom`/);
});
