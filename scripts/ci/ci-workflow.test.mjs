// What must stay true of .github/workflows/ci.yml for the path rule to be safe.
//
// Run by the `changes` job before it decides anything, without any install:
// the workflow is read as text, job by job. These are the properties a later
// edit of the workflow could break without any gate turning red.

import assert from "node:assert/strict";
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

const REQUIRED = ["pi-gate", "simulation-gate", "convex-tests", "web", "audit", "docs"];
const ALWAYS = ["changes", "audit", "docs"];
const condition = (/** @type {string} */ output) =>
  `\${{ !cancelled() && (github.event_name != 'pull_request' || needs.changes.outputs.${output} != 'false') }}`;
const GATED = new Map([
  ["pi-gate", "python"],
  ["simulation-battery", "python"],
  ["simulation-report", "python"],
  ["simulation-gate", "python"],
  ["convex-tests", "node"],
  ["web", "node"],
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
  for (const [id, output] of GATED) {
    assert.equal(own(id, "if"), condition(output), id);
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

test("the simulation gate asks how every part ended before it judges their records", () => {
  const gate = jobs.get("simulation-gate") ?? "";
  assert.equal(own("simulation-gate", "needs"), "[changes, simulation-battery, simulation-report]");
  assert.match(gate, /BATTERY: \$\{\{ needs\.simulation-battery\.result \}\}/);
  assert.match(gate, /REPORT: \$\{\{ needs\.simulation-report\.result \}\}/);
  assert.match(gate, /\[\[ "\$BATTERY" == success && "\$REPORT" == success \]\]/);
  assert.match(gate, /SIMULATION_GATE_COMBINE: \$\{\{ env\.SIMULATION_SHARES \}\}/);
});
