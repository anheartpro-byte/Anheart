// What must stay true of .github/workflows/ci.yml for the path rule to be safe.
//
// Run by the `changes` job before it decides anything, without any install:
// the workflow is read as text, job by job. These are the properties a later
// edit of the workflow could break without any gate turning red.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { readFileSync } from "node:fs";
import test from "node:test";

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
  assert.deepEqual([...jobs.keys()].sort(), [...ALWAYS, ...GATED.keys()].sort());
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
  assert.ok(typecheck > 0 && typecheck < steps.indexOf("run: npm run test:convex\n"));
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
