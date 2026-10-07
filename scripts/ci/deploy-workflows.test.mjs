// What must stay true of the two buttons that deploy to Vercel,
// .github/workflows/deploy-production.yml and deploy-preview.yml, and of the
// `vercel.json` that keeps every push from deploying.
//
// Run by the `changes` job of ci.yml with the other CI test files, on every
// run and without any install: the workflows are read as text, and the
// scripts of their steps are run as the runner would run them, with a double
// in place of the Vercel CLI. Nothing here calls Vercel. A real deployment
// cannot be tried from a test: these tests hold what a later edit could break
// without any job turning red.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));
const WORKFLOWS = ".github/workflows";

/** @param {string} path relative to the repository root */
const read = (path) => readFileSync(join(root, path), "utf8");
/** A workflow without its comment lines: what GitHub acts on. @param {string} file */
const workflow = (file) =>
  read(`${WORKFLOWS}/${file}`)
    .split("\n")
    .filter((line) => !line.trimStart().startsWith("#"))
    .join("\n");

/** The two buttons, and what tells one from the other. */
const BUTTONS = [
  {
    file: "deploy-production.yml",
    name: "Déployer en production (main)",
    branch: "main",
    target: "production",
    shown: "production",
    inputs: ["project", "confirmation"],
    pull: "pull --yes --environment=production",
    build: "build --prod",
    site: "deploy --prebuilt --archive=tgz --prod",
    simulation: "deploy --yes --prod",
  },
  {
    file: "deploy-preview.yml",
    name: "Déployer la préversion (develop)",
    branch: "develop",
    target: "preview",
    shown: "préversion",
    inputs: ["project"],
    pull: "pull --yes --environment=preview --git-branch=develop",
    build: "build",
    site: "deploy --prebuilt --archive=tgz",
    simulation: "deploy --yes",
  },
].map((button) => ({ ...button, text: workflow(button.file) }));
const [PRODUCTION, PREVIEW] = BUTTONS;
assert.ok(PRODUCTION && PREVIEW);

const CHOICES = ["site", "simulation", "site et simulation"];
const SECRETS = ["VERCEL_ORG_ID", "VERCEL_PROJECT_ID_SIMULATION", "VERCEL_PROJECT_ID_SITE", "VERCEL_TOKEN"];
/** The steps a job is made of, in order: a name, or the action a step uses. */
const STEPS = {
  request: ["Refuser toute autre branche"],
  site: [
    "Exiger les secrets du déploiement",
    "uses actions/checkout",
    "uses actions/setup-node",
    "Installer la CLI Vercel à sa version figée",
    "Lire les réglages et les variables du projet Vercel",
    "Construire le site",
    "Déployer la construction du site",
    "Annoncer le commit et l'adresse",
  ],
  simulation: [
    "Exiger les secrets du déploiement",
    "uses actions/checkout",
    "uses actions/setup-node",
    "Installer la CLI Vercel à sa version figée",
    "Assembler la simulation",
    "Déployer la simulation assemblée",
    "Annoncer le commit et l'adresse",
  ],
};
const CONFIRM = "Exiger la confirmation saisie";
/** The only steps that are handed the token: those that call Vercel, and the one that checks it is there. */
const WITH_TOKEN = {
  request: [],
  site: ["Exiger les secrets du déploiement", "Lire les réglages et les variables du projet Vercel", "Déployer la construction du site"],
  simulation: ["Exiger les secrets du déploiement", "Déployer la simulation assemblée"],
};

/**
 * The lines nested under a key, trimmed.
 * @param {string} text @param {string} key the key, with its indentation
 */
const block = (text, key) => {
  const indent = /^ */.exec(key)?.[0].length ?? 0;
  const nested = new RegExp(`^${key}:[ \\t]*\\n((?: {${indent + 2}}.*\\n)+)`, "m").exec(text)?.[1] ?? "";
  return nested.split("\n").filter(Boolean).map((line) => line.trim());
};

/** The keys written directly under a key. @param {string} text @param {string} key the key, with its indentation */
const keysUnder = (text, key) => {
  const indent = /^ */.exec(key)?.[0].length ?? 0;
  const nested = new RegExp(`^${key}:[ \\t]*\\n((?: {${indent + 2}}.*\\n)+)`, "m").exec(text)?.[1] ?? "";
  return [...nested.matchAll(new RegExp(`^ {${indent + 2}}([A-Za-z_-]+):`, "gm"))].map((match) => match[1]);
};

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

/** The steps of a job, each as its own lines. @param {string} job */
const stepsOf = (job) => job.split(/^ {6}- /m).slice(1);
/** @param {string} step */
const labelOf = (step) => /^name: (.*)\n/.exec(step)?.[1] ?? `uses ${/^uses: ([^@\s]+)@/.exec(step)?.[1]}`;
/** @param {string} job @param {string} name */
const stepNamed = (job, name) => {
  const step = stepsOf(job).find((text) => labelOf(text) === name);
  assert.ok(step, `no step "${name}"`);
  return step;
};
/** The shell text of a step, as the runner would be handed it. @param {string} job @param {string} name */
const script = (job, name) => {
  const step = stepNamed(job, name);
  const written = /^ {8}run: \|\n((?: {10}.*\n|\n)+)/m.exec(step)?.[1];
  const line = /^ {8}run: (?!\|)(.+)\n/m.exec(step)?.[1];
  assert.ok(written ?? line, `the step "${name}" runs nothing`);
  // Without the blank lines that separate the last step of a job from the next job.
  return written ? written.replaceAll(/^ {10}/gm, "").replace(/\n+$/, "\n") : `${line}\n`;
};

/** What `env:` gives every script of a workflow. @param {string} text @returns {Record<string, string>} */
const shared = (text) =>
  Object.fromEntries(
    block(text, "env").map((line) => {
      const [, key = "", value = ""] = /^([A-Z_]+): '?([^']*)'?$/.exec(line) ?? [];
      return [key, value];
    }),
  );

/** A directory that holds a double of the Vercel CLI: it writes down how it was called, and answers as told. */
const scratch = () => {
  const directory = mkdtempSync(join(tmpdir(), "anheart-deploy-"));
  writeFileSync(
    join(directory, "vercel"),
    [
      "#!/usr/bin/env bash",
      'printf \'%s|%s|%s\\n\' "$*" "${VERCEL_TOKEN:-none}" "${VERCEL_PROJECT_ID:-none}" >> "$DOUBLE_CALLS"',
      'if [[ "$1" == deploy ]]; then printf \'%s\' "${DOUBLE_ANSWER:-}"; fi',
      'exit "${DOUBLE_STATUS:-0}"',
      "",
    ].join("\n"),
  );
  chmodSync(join(directory, "vercel"), 0o755);
  return directory;
};

/**
 * Runs a script the way a GitHub runner runs a `run:` step with `shell: bash`.
 * @param {string} text the script @param {Record<string, string>} env @param {string} [cwd]
 */
const run = (text, env, cwd = root) => {
  const directory = scratch();
  const files = { calls: join(directory, "calls"), output: join(directory, "output"), summary: join(directory, "summary") };
  for (const file of Object.values(files)) writeFileSync(file, "");
  const result = spawnSync("bash", ["--noprofile", "--norc", "-e", "-o", "pipefail", "-c", text], {
    cwd,
    encoding: "utf8",
    env: {
      PATH: `${directory}:${process.env.PATH ?? ""}`,
      HOME: process.env.HOME ?? "",
      DOUBLE_CALLS: files.calls,
      GITHUB_OUTPUT: files.output,
      GITHUB_STEP_SUMMARY: files.summary,
      ...env,
    },
  });
  const calls = readFileSync(files.calls, "utf8").split("\n").filter(Boolean);
  const [output, summary] = [readFileSync(files.output, "utf8"), readFileSync(files.summary, "utf8")];
  rmSync(directory, { recursive: true, force: true });
  return { status: result.status, said: `${result.stdout}${result.stderr}`, calls, output, summary };
};

const ADDRESS = "https://anheart-abc123-anhearts-projects.vercel.app";
/** Stand-ins with a space in them: no secret looks like these, and a script that printed one would show. */
const HELD = { VERCEL_TOKEN: "jeton de test", VERCEL_ORG_ID: "equipe de test", VERCEL_PROJECT_ID: "projet de test" };

test("no push deploys: the configuration both Vercel projects read turns Git deployments off for every branch", () => {
  const config = JSON.parse(read("vercel.json"));
  // `false`, not a list of branches: a list leaves every branch it does not name deploying.
  assert.deepEqual(config.git, { deploymentEnabled: false });
  // Both projects build from the root of the repository and read this one
  // file: a key added for one of them applies to the other. Nothing else is in it.
  assert.deepEqual(Object.keys(config).sort(), ["$schema", "git"]);
  // No Vercel configuration written as code would be read in its place.
  for (const other of ["vercel.ts", "vercel.mts", "vercel.js", "vercel.mjs", "now.json"]) {
    assert.ok(!existsSync(join(root, other)), `${other} competes with vercel.json`);
  }
});

test("each button has its French name, and starts by hand and from nothing else", () => {
  for (const { file, name, text, inputs } of BUTTONS) {
    assert.equal(text.split("\n")[0], `name: ${name}`, file);
    // `on:` is written once, as a block, and holds one event.
    assert.deepEqual(text.match(/^on:.*$/gm), ["on:"], file);
    assert.deepEqual(keysUnder(text, "on"), ["workflow_dispatch"], file);
    assert.doesNotMatch(text, /pull_request|workflow_run|workflow_call|repository_dispatch|deployment_status|schedule/, file);
    assert.deepEqual(keysUnder(text, "    inputs"), inputs, file);
    assert.deepEqual(block(text, "      project").slice(1), ["required: true", "type: choice", "options:", ...CHOICES.map((choice) => `- ${choice}`)], file);
  }
});

test("the jobs and their steps are the ones this file knows about, the same behind both buttons", () => {
  for (const { file, text, shown } of BUTTONS) {
    const jobs = jobsOf(text);
    assert.deepEqual([...jobs.keys()], ["request", "site", "simulation"], file);
    for (const [id, expected] of Object.entries(STEPS)) {
      const steps = stepsOf(jobs.get(id) ?? "").map(labelOf);
      assert.deepEqual(steps.filter((label) => label !== CONFIRM), expected, `${file}: ${id}`);
      assert.equal(own(jobs.get(id) ?? "", "runs-on"), "ubuntu-24.04", `${file}: ${id}`);
      assert.match(own(jobs.get(id) ?? "", "timeout-minutes") ?? "", /^\d+$/, `${file}: ${id}`);
    }
    assert.deepEqual(
      [...jobs.values()].map((job) => own(job, "name")),
      [`Vérifier la demande (${shown})`, `Déployer le site (${shown})`, `Déployer la simulation (${shown})`],
      file,
    );
  }
  // The two files differ by what `env:` holds, never by what a step runs.
  const [production, preview] = [jobsOf(PRODUCTION.text), jobsOf(PREVIEW.text)];
  for (const [id, names] of Object.entries(STEPS)) {
    for (const name of names.filter((label) => !label.startsWith("uses "))) {
      assert.equal(script(production.get(id) ?? "", name), script(preview.get(id) ?? "", name), `${id}: ${name}`);
    }
  }
  assert.deepEqual(Object.keys(shared(PRODUCTION.text)), ["DEPLOY_BRANCH", "VERCEL_TARGET", "VERCEL_CLI_VERSION"]);
  assert.deepEqual(Object.keys(shared(PREVIEW.text)), Object.keys(shared(PRODUCTION.text)));
});

test("a button refuses every ref but its own branch, before anything else runs", () => {
  for (const { file, text, branch, target } of BUTTONS) {
    const jobs = jobsOf(text);
    const request = jobs.get("request") ?? "";
    assert.deepEqual(shared(text).DEPLOY_BRANCH, branch, file);
    assert.deepEqual(shared(text).VERCEL_TARGET, target, file);
    // The guard is the first step of the first job, is handed the ref the run started on, and waits for no approval.
    assert.equal(labelOf(stepsOf(request)[0] ?? ""), "Refuser toute autre branche", file);
    assert.deepEqual(block(stepNamed(request, "Refuser toute autre branche"), "        env"), ["REF: ${{ github.ref }}"], file);
    assert.equal(own(request, "environment"), undefined, file);
    assert.equal(own(request, "needs"), undefined, file);
    assert.equal(own(request, "if"), undefined, file);
    assert.doesNotMatch(request, /secrets\./, file);
    // No deployment job starts unless that job succeeded: no status function may lift the default.
    for (const [id, wanted] of [["site", "site"], ["simulation", "simulation"]]) {
      const job = jobs.get(id) ?? "";
      assert.equal(own(job, "needs"), "request", `${file}: ${id}`);
      assert.equal(own(job, "if"), `\${{ inputs.project == '${wanted}' || inputs.project == 'site et simulation' }}`, `${file}: ${id}`);
    }
    assert.doesNotMatch(text, /always\(\)|cancelled\(\)|failure\(\)|continue-on-error/, file);

    const guard = script(request, "Refuser toute autre branche");
    const accepted = run(guard, { ...shared(text), REF: `refs/heads/${branch}` });
    assert.equal(accepted.status, 0, `${file}: ${accepted.said}`);
    const other = branch === "main" ? "develop" : "main";
    for (const ref of [
      `refs/heads/${other}`,
      `refs/heads/feature/${branch}`,
      `refs/heads/${branch}-2`,
      `refs/heads/x/${branch}`,
      `refs/tags/${branch}`,
      "refs/pull/41/merge",
      branch,
      "",
    ]) {
      const refused = run(guard, { ...shared(text), REF: ref });
      assert.equal(refused.status, 1, `${file}: "${ref}" was accepted`);
      assert.match(refused.said, new RegExp(`^::error title=Mauvaise branche::Rien n'a été déployé\\. Ce bouton ne déploie que la branche ${branch} `, "m"), file);
    }
  }
});

test("production asks for a typed confirmation, the preview for none", () => {
  const request = jobsOf(PRODUCTION.text).get("request") ?? "";
  assert.deepEqual(stepsOf(request).map(labelOf), ["Refuser toute autre branche", CONFIRM]);
  assert.deepEqual(block(PRODUCTION.text, "      confirmation").slice(1), ["required: true", "type: string"]);
  assert.deepEqual(block(stepNamed(request, CONFIRM), "        env"), ["CONFIRMATION: ${{ inputs.confirmation }}"]);
  const confirm = script(request, CONFIRM);
  assert.equal(run(confirm, { ...shared(PRODUCTION.text), CONFIRMATION: "production" }).status, 0);
  for (const typed of ["", "Production", "PRODUCTION", "production ", " production", "prod", "oui", "main", "preview", "production\nproduction"]) {
    const refused = run(confirm, { ...shared(PRODUCTION.text), CONFIRMATION: typed });
    assert.equal(refused.status, 1, `"${typed}" was accepted`);
    assert.match(refused.said, /^::error title=Confirmation refusée::Rien n'a été déployé\. /m);
  }
  assert.doesNotMatch(PREVIEW.text, /confirmation|CONFIRMATION/);
});

test("each deployment goes through its GitHub environment, and two of the same target never overlap", () => {
  const groups = [];
  for (const { file, text, target } of BUTTONS) {
    for (const id of ["site", "simulation"]) {
      const job = jobsOf(text).get(id) ?? "";
      assert.deepEqual(block(job, "    environment"), [`name: ${target}`, "url: ${{ steps.deploy.outputs.url }}"], `${file}: ${id}`);
      // The second deployment waits for the first: one in progress is never cancelled.
      assert.deepEqual(block(job, "    concurrency"), [`group: vercel-${target}-${id}`, "cancel-in-progress: false"], `${file}: ${id}`);
      groups.push(`vercel-${target}-${id}`);
    }
    assert.equal(text.split("environment:").length - 1, 2, file);
    assert.equal(text.split("concurrency:").length - 1, 2, file);
  }
  assert.equal(new Set(groups).size, 4);
});

test("no job can write to the repository, and none can be taken for a job of ci.yml", () => {
  const taken = new Set();
  for (const [id, job] of jobsOf(workflow("ci.yml"))) {
    taken.add(id);
    taken.add(own(job, "name") ?? id);
  }
  assert.ok(taken.has("pi-gate") && taken.has("docs"), "the jobs of ci.yml were not read");
  for (const { file, text } of BUTTONS) {
    assert.deepEqual(block(text, "permissions"), ["contents: read"], file);
    assert.equal(text.split("permissions:").length - 1, 1, `${file}: a job sets its own permissions`);
    assert.doesNotMatch(text, /write|id-token/, file);
    for (const [id, job] of jobsOf(text)) {
      for (const shown of [id, own(job, "name") ?? id]) assert.ok(!taken.has(shown), `${file}: "${shown}" is a job of ci.yml`);
    }
  }
});

test("every action is one ci.yml already uses, pinned to the same full commit, and nothing is cached", () => {
  /** @param {string} text */
  const actions = (text) =>
    [...text.matchAll(/^ +(?:- )?uses:[ \t]*(.*)$/gm)].map(([, written = ""]) => {
      const parts = /^([\w.-]+\/[\w./-]+)@([0-9a-f]{40}) # (v\d+\.\d+\.\d+)$/.exec(written);
      return { written, action: parts?.[1], pin: parts ? `${parts[2]} ${parts[3]}` : undefined };
    });
  const reviewed = new Map(actions(workflow("ci.yml")).map(({ action, pin }) => [action, pin]));
  assert.ok(reviewed.has("actions/checkout") && reviewed.has("actions/setup-node"), "the actions of ci.yml were not read");
  for (const { file, text } of BUTTONS) {
    const used = actions(text);
    assert.deepEqual(used.map(({ action }) => action), ["actions/checkout", "actions/setup-node", "actions/checkout", "actions/setup-node"], file);
    for (const { written, action, pin } of used) {
      assert.ok(pin, `${file}: "uses: ${written}" is not <action>@<40 hex> # vX.Y.Z`);
      assert.equal(pin, reviewed.get(action), `${file}: ${action}`);
    }
    // The checkout leaves no token in the Git configuration, and no cache is restored into a deployment.
    assert.equal(text.split("persist-credentials: false").length - 1, 2, file);
    assert.doesNotMatch(text, /cache/, file);
  }
});

test("the Vercel CLI is installed at one exact version, the same behind both buttons", () => {
  const version = shared(PRODUCTION.text).VERCEL_CLI_VERSION ?? "";
  assert.match(version, /^[1-9]\d*\.\d+\.\d+$/, "an exact version, not a range, a tag or a prerelease");
  for (const { file, text } of BUTTONS) {
    assert.equal(shared(text).VERCEL_CLI_VERSION, version, file);
    assert.match(text, new RegExp(`^ {2}VERCEL_CLI_VERSION: '${version.replaceAll(".", "\\.")}'$`, "m"), file);
    for (const id of ["site", "simulation"]) {
      const install = script(jobsOf(text).get(id) ?? "", "Installer la CLI Vercel à sa version figée");
      assert.equal(install, 'npm install --global "vercel@$VERCEL_CLI_VERSION"\n', `${file}: ${id}`);
    }
    // The CLI comes from that step alone: no other version, no `npx`, no moving tag.
    assert.equal(text.split("vercel@").length - 1, 2, file);
    assert.doesNotMatch(text, /npx|@latest|@canary/, file);
  }
});

test("the secrets are read by name through env, the token by the steps that call Vercel, and no script holds an expression", () => {
  for (const { file, text } of BUTTONS) {
    const named = [...text.matchAll(/secrets\.([A-Za-z_]+)/g)].map((match) => match[1]);
    assert.deepEqual([...new Set(named)].sort(), SECRETS, file);
    // An expression is a whole value of `if:`, of `url:` or of an `env:` entry, and nothing else.
    for (const line of text.split("\n").filter((written) => written.includes("${{"))) {
      assert.match(
        line,
        /^ {4}if: \$\{\{ inputs\.project == '[a-z ]+' \|\| inputs\.project == '[a-z ]+' \}\}$|^ {6}url: \$\{\{ steps\.deploy\.outputs\.url \}\}$|^ {6,10}[A-Z_]+: \$\{\{ (secrets\.[A-Z_]+|github\.ref|inputs\.confirmation|steps\.deploy\.outputs\.url) \}\}$/,
        `${file}: ${line.trim()}`,
      );
    }
    for (const [id, job] of jobsOf(text)) {
      const steps = stepsOf(job);
      for (const step of steps.filter((written) => /^ {8}run:/m.test(written))) {
        assert.doesNotMatch(script(job, labelOf(step)), /\$\{\{|secrets\.|inputs\./, `${file}: ${id}: ${labelOf(step)}`);
      }
      const handed = steps.filter((step) => step.includes("secrets.VERCEL_TOKEN")).map(labelOf);
      assert.deepEqual(handed, WITH_TOKEN[/** @type {keyof typeof WITH_TOKEN} */ (id)], `${file}: ${id}`);
      for (const step of steps.filter((written) => written.includes("secrets.VERCEL_TOKEN"))) {
        assert.match(step, /^ {10}VERCEL_TOKEN: \$\{\{ secrets\.VERCEL_TOKEN \}\}$/m, `${file}: ${id}`);
      }
    }
    // The two identifiers are given to the whole job, each job with the identifier of its own project.
    for (const [id, project] of [["site", "VERCEL_PROJECT_ID_SITE"], ["simulation", "VERCEL_PROJECT_ID_SIMULATION"]]) {
      const job = jobsOf(text).get(id) ?? "";
      assert.deepEqual(block(job, "    env"), ["VERCEL_ORG_ID: ${{ secrets.VERCEL_ORG_ID }}", `VERCEL_PROJECT_ID: \${{ secrets.${project} }}`], `${file}: ${id}`);
      assert.match(stepNamed(job, "Exiger les secrets du déploiement"), new RegExp(`^ {10}PROJECT_ID_NAME: ${project}$`, "m"), `${file}: ${id}`);
    }
  }
});

test("a missing secret fails the job at once, by its name, and no value is ever printed", () => {
  for (const { file, text } of BUTTONS) {
    for (const [id, project] of [["site", "VERCEL_PROJECT_ID_SITE"], ["simulation", "VERCEL_PROJECT_ID_SIMULATION"]]) {
      const job = jobsOf(text).get(id) ?? "";
      assert.equal(labelOf(stepsOf(job)[0] ?? ""), "Exiger les secrets du déploiement", `${file}: ${id}: it must be the first step`);
      const check = script(job, "Exiger les secrets du déploiement");
      const given = { ...shared(text), ...HELD, PROJECT_ID_NAME: project };
      const present = run(check, given);
      assert.equal(present.status, 0, `${file}: ${id}: ${present.said}`);
      for (const value of Object.values(HELD)) assert.ok(!present.said.includes(value), `${file}: ${id}: a value was printed`);
      for (const [variable, secret] of [["VERCEL_TOKEN", "VERCEL_TOKEN"], ["VERCEL_ORG_ID", "VERCEL_ORG_ID"], ["VERCEL_PROJECT_ID", project]]) {
        // What GitHub gives for a secret that does not exist: an empty value.
        const refused = run(check, { ...given, [variable]: "" });
        assert.equal(refused.status, 1, `${file}: ${id}: an empty ${secret} was accepted`);
        assert.match(refused.said, new RegExp(`^::error title=Secret manquant::Rien n'a été déployé\\. Secret GitHub absent ou vide : ${secret}\\. `, "m"), `${file}: ${id}`);
        for (const value of Object.values(HELD)) assert.ok(!refused.said.includes(value), `${file}: ${id}: a value was printed`);
      }
      const none = run(check, { ...shared(text), VERCEL_TOKEN: "", VERCEL_ORG_ID: "", VERCEL_PROJECT_ID: "", PROJECT_ID_NAME: project });
      assert.match(none.said, new RegExp(`absent ou vide : VERCEL_TOKEN VERCEL_ORG_ID ${project}\\. `), `${file}: ${id}`);
    }
  }
});

test("the site is pulled, built and deployed prebuilt for the target of its button, and the build never holds the token", () => {
  for (const { file, text, pull, build, site } of BUTTONS) {
    const job = jobsOf(text).get("site") ?? "";
    const given = { ...shared(text), VERCEL_ORG_ID: HELD.VERCEL_ORG_ID, VERCEL_PROJECT_ID: HELD.VERCEL_PROJECT_ID };
    const pulled = run(script(job, "Lire les réglages et les variables du projet Vercel"), { ...given, VERCEL_TOKEN: HELD.VERCEL_TOKEN });
    assert.equal(pulled.status, 0, `${file}: ${pulled.said}`);
    assert.deepEqual(pulled.calls, [`${pull}|${HELD.VERCEL_TOKEN}|${HELD.VERCEL_PROJECT_ID}`], file);
    // The runner gives a step only the env written on it: this one is written none of the token.
    assert.doesNotMatch(stepNamed(job, "Construire le site"), /env:|secrets\./, file);
    assert.doesNotMatch(stepNamed(job, "Installer la CLI Vercel à sa version figée"), /env:|secrets\./, file);
    const built = run(script(job, "Construire le site"), given);
    assert.equal(built.status, 0, `${file}: ${built.said}`);
    assert.deepEqual(built.calls, [`${build}|none|${HELD.VERCEL_PROJECT_ID}`], file);
    const deployed = run(script(job, "Déployer la construction du site"), { ...given, VERCEL_TOKEN: HELD.VERCEL_TOKEN, DOUBLE_ANSWER: ADDRESS });
    assert.equal(deployed.status, 0, `${file}: ${deployed.said}`);
    assert.deepEqual(deployed.calls, [`${site}|${HELD.VERCEL_TOKEN}|${HELD.VERCEL_PROJECT_ID}`], file);
    assert.equal(deployed.output, `url=${ADDRESS}\n`, file);
    for (const said of [pulled.said, built.said, deployed.said]) assert.ok(!said.includes(HELD.VERCEL_TOKEN), `${file}: the token was printed`);
    assert.match(stepNamed(job, "Déployer la construction du site"), /^ {8}id: deploy$/m, file);
  }
});

test("the simulation is assembled by its own build.sh and deployed from dist/, as deploy.sh does", () => {
  assert.ok(existsSync(join(root, "deploy/simulation-vercel/build.sh")));
  assert.match(read("deploy/simulation-vercel/build.sh"), /^dist="\$here\/dist"$/m, "build.sh no longer assembles dist/");
  for (const { file, text, branch, simulation } of BUTTONS) {
    const job = jobsOf(text).get("simulation") ?? "";
    const assemble = script(job, "Assembler la simulation");
    assert.ok(assemble.endsWith("\nbash deploy/simulation-vercel/build.sh\n"), file);
    assert.doesNotMatch(stepNamed(job, "Assembler la simulation"), /env:|secrets\./, file);
    // On a branch that does not hold the simulation, the refusal says so instead of a missing file.
    const elsewhere = mkdtempSync(join(tmpdir(), "anheart-empty-"));
    const absent = run(assemble, shared(text), elsewhere);
    rmSync(elsewhere, { recursive: true, force: true });
    assert.equal(absent.status, 1, file);
    assert.match(absent.said, new RegExp(`^::error title=Simulation absente::Rien n'a été déployé\\. La branche ${branch} ne contient pas `, "m"), file);

    const step = stepNamed(job, "Déployer la simulation assemblée");
    assert.match(step, /^ {8}id: deploy$/m, file);
    assert.match(step, /^ {8}working-directory: deploy\/simulation-vercel\/dist$/m, file);
    const given = { ...shared(text), ...HELD, DOUBLE_ANSWER: ADDRESS };
    const deployed = run(script(job, "Déployer la simulation assemblée"), given);
    assert.equal(deployed.status, 0, `${file}: ${deployed.said}`);
    assert.deepEqual(deployed.calls, [`${simulation}|${HELD.VERCEL_TOKEN}|${HELD.VERCEL_PROJECT_ID}`], file);
    assert.equal(deployed.output, `url=${ADDRESS}\n`, file);
    assert.ok(!deployed.said.includes(HELD.VERCEL_TOKEN), `${file}: the token was printed`);
  }
});

test("a deployment that fails, or that gives no address, fails its job and announces nothing", () => {
  for (const { file, text } of BUTTONS) {
    for (const [id, name] of [["site", "Déployer la construction du site"], ["simulation", "Déployer la simulation assemblée"]]) {
      const deploy = script(jobsOf(text).get(id) ?? "", name);
      const given = { ...shared(text), ...HELD };
      const failed = run(deploy, { ...given, DOUBLE_ANSWER: ADDRESS, DOUBLE_STATUS: "1" });
      assert.notEqual(failed.status, 0, `${file}: ${id}: a failed deployment passed`);
      assert.equal(failed.output, "", `${file}: ${id}`);
      for (const answer of ["", "Error: rate limited", `${ADDRESS}\nsecond line`, "http://anheart.vercel.app", `${ADDRESS}/path?x=1`]) {
        const refused = run(deploy, { ...given, DOUBLE_ANSWER: answer });
        assert.equal(refused.status, 1, `${file}: ${id}: "${answer}" was taken for an address`);
        assert.match(refused.said, /^::error title=Adresse absente::/m, `${file}: ${id}`);
        assert.equal(refused.output, "", `${file}: ${id}`);
      }
    }
  }
});

const head = spawnSync("git", ["rev-parse", "HEAD"], { cwd: root, encoding: "utf8" });
const commit = head.status === 0 ? head.stdout.trim() : "";

test("the summary of a run gives the commit deployed and the address obtained", { skip: commit === "" }, () => {
  for (const { file, text, branch, target } of BUTTONS) {
    for (const [id, deployed] of [["site", "Site"], ["simulation", "Simulation"]]) {
      const step = stepNamed(jobsOf(text).get(id) ?? "", "Annoncer le commit et l'adresse");
      assert.deepEqual(block(step, "        env"), [`DEPLOYED: ${deployed}`, "URL: ${{ steps.deploy.outputs.url }}"], `${file}: ${id}`);
      const announced = run(script(jobsOf(text).get(id) ?? "", "Annoncer le commit et l'adresse"), { ...shared(text), DEPLOYED: deployed, URL: ADDRESS });
      assert.equal(announced.status, 0, `${file}: ${id}: ${announced.said}`);
      assert.equal(
        announced.summary,
        [
          `### ${deployed} : déployé sur Vercel (${target})`,
          "",
          `- Commit déployé : \`${commit}\` (tête de \`${branch}\` au lancement)`,
          `- Adresse du déploiement : ${ADDRESS}`,
          "",
        ].join("\n"),
        `${file}: ${id}`,
      );
    }
  }
});

test("the changes job of ci.yml runs this file", () => {
  const changes = jobsOf(workflow("ci.yml")).get("changes") ?? "";
  assert.match(changes, /^ {8}run: node --test .*\bscripts\/ci\/deploy-workflows\.test\.mjs\b/m);
});
